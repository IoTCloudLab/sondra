"""Core data document types.
"""

from collections.abc import MutableMapping, Mapping
from abc import ABCMeta
from copy import deepcopy, copy
from functools import partial
from urllib.parse import urlparse
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
import json
import jsonschema
import rethinkdb as r
from datetime import date, datetime, timezone
import logging
import logging.config
import iso8601

from . import utils
from .ref import Reference


DOCSTRING_PROCESSORS = {}
try:
    from docutils.core import publish_string
    from sphinxcontrib import napoleon

    def google_processor(s):
        return publish_string(str(napoleon.GoogleDocstring(s)), writer_name='html')

    def numpy_processor(s):
        return publish_string(str(napoleon.NumpyDocstring(s)), writer_name='html')

    DOCSTRING_PROCESSORS['google'] = google_processor
    DOCSTRING_PROCESSORS['numpy'] = numpy_processor
except ImportError:
    pass

try:
    from docutils.core import publish_string

    DOCSTRING_PROCESSORS['rst'] = partial(publish_string, writer_name='html')
except ImportError:
    pass

try:
    from markdown import markdown

    DOCSTRING_PROCESSORS['markdown'] = markdown
except ImportError:
    pass

DOCSTRING_PROCESSORS['preformatted'] = lambda x: "<pre>" + str(x) + "</pre>"

_validator = jsonschema.Draft4Validator


BASIC_TYPES = {
    "date": {
        "type": "object",
        "required": ["year"],
        "properties": {
            "year": {"type": "integer"},
            "month": {"type": "integer"},
            "day": {"type": "integer"}
        }
    },
    "datetime": {
        "type": "object",
        "allOf": ["#/definitions/date"],
        "required": ["year","month","day","hour"],
        "properties": {
            "hour": {"type": "integer"},
            "minute": {"type": "integer"},
            "second": {"type": "float"},
            "timezone": {"type": "string", "default": "Z"}
        }
    },
    "timedelta": {
        "type": "object",
        "required": ["start", "end"],
        "properties": {
            "start": {"$ref": "#/definitions/datetime"},
            "end": {"$ref": "#/definitions/datetime"},
        },
        "definitions": {
            "datetime": {
                "type": "object",
                "allOf": ["#/definitions/date"],
                "required": ["year","month","day","hour"],
                "properties": {
                    "hour": {"type": "integer"},
                    "minute": {"type": "integer"},
                    "second": {"type": "float"},
                    "timezone": {"type": "string", "default": "Z"}
                }
            }
        }
    }
}


def _to_ref(doc):
    if isinstance(doc, Document):
        return doc.url
    else:
        return doc

def _from_ref(doc):
    env = Suite()
    if isinstance(doc, str):
        if doc.startswith(env.base_url):
            return env.lookup_document(doc)
        else:
            return doc
    else:
        return doc

references = partial(utils.mapjson, _to_ref)
documents = partial(utils.mapjson, _from_ref)

class ValidationError(Exception):
    """This kind of validation error is thrown whenever an :class:`Application` or :class:`Collection` is
    misconfigured."""

class ValueHandler(object):
    """This is base class for transforming values to/from RethinkDB representations to standard representations.

    Attributes:
        is_geometry (bool): Does this handle geometry/geographical values. Indicates to Sondra that indexing should
            be handled differently.
    """
    is_geometry = False

    def to_rql_repr(self, value):
        """Transform the object value into a ReQL object for storage.

        Args:
            value: The value to transform

        Returns:
            object: A ReQL object.
        """
        return value

    def to_json_repr(self, value):
        """Transform the object from a ReQL value into a standard value.

        Args:
            value (ReQL): The value to transform

        Returns:
            dict: A Python object representing the value.
        """
        return value

    def to_python_repr(self, value):
        """Transform the object from a ReQL value into a standard value.

        Args:
            value (ReQL): The value to transform

        Returns:
            dict: A Python object representing the value.
        """
        return value


class Geometry(ValueHandler):
    """A value handler for GeoJSON"""
    is_geometry = True

    def __init__(self, *allowed_types):
        self.allowed_types = set(x.lower() for x in allowed_types) if allowed_types else None

    def to_rql_repr(self, value):
        if self.allowed_types:
            if value['type'].lower() not in self.allowed_types:
                raise ValidationError('value not in ' + ','.join(t for t in self.allowed_types))
        return r.geojson(value)

    def to_json_repr(self, value):
        if isinstance(value, BaseGeometry):
            return mapping(value)
        else:
            return value

    def to_python_repr(self, value):
        del value['$reql_type$']
        return shape(value)


class Time(ValueHandler):
    DEFAULT_TIMEZONE='Z'
    """A valuehandler for Python datetimes"""
    def __init__(self, timezone='Z'):
        self.timezone = timezone

    def from_rql_tz(self, tz):
        if tz == 'Z':
            return 0
        else:
            posneg = -1 if tz[0] == '-' else 1
            hours, minutes = map(int, tz.split(":"))
            offset = posneg*(hours*60 + minutes)
            return offset

    def to_rql_repr(self, value):
        if isinstance(value, str):
            return r.iso8601(value, default_timezone=self.DEFAULT_TIMEZONE).in_timezone(self.timezone)
        elif isinstance(value, int) or isinstance(value, float):
            return datetime.fromtimestamp(value).isoformat()
        elif isinstance(value, dict):
            return r.time(
                value.get('year', None),
                value.get('month', None),
                value.get('day', None),
                value.get('hour', None),
                value.get('minute', None),
                value.get('second', None),
                value.get('timezone', self.timezone),
            ).in_timezone(self.timezone)
        else:
            return r.iso8601(value.isoformat(), default_timezone=self.DEFAULT_TIMEZONE).as_timezone(self.timezone)

    def to_json_repr(self, value):
        if isinstance(value, date) or isinstance(value, datetime):
            return value.isoformat()
        elif hasattr(value, 'to_epoch_time'):
            return value.to_iso8601()
        elif isinstance(value, int) or isinstance(value, float):
            return datetime.fromtimestamp(value).isoformat()
        else:
            return value

    def to_python_repr(self, value):
        if isinstance(value, str):
            return iso8601.parse_date(value)
        elif isinstance(value, datetime):
            return value
        elif hasattr(value, 'to_epoch_time'):
            timestamp = value.to_epoch_time()
            tz = value.timezone()
            offset = self.from_rql_tz(tz)
            offset_tz = timezone(offset)
            dt = datetime.fromtimestamp(timestamp, tz=offset_tz)
            return dt


class SuiteException(Exception):
    """Represents a misconfiguration in a :class:`Suite` class"""


class ApplicationException(Exception):
    """Represents a misconfiguration in an :class:`Application` class definition"""


class CollectionException(Exception):
    """Represents a misconfiguration in a :class:`Collection` class definition"""


class Singleton(type):
    """Define a singleton. This Suite class is a singleton."""
    instance = None
    def __call__(cls, *args, **kw):
        if not cls.instance:
             cls.instance = super(Singleton, cls).__call__(*args, **kw)
        return cls.instance


class SuiteMetaclass(ABCMeta):
    """This special bit of kit transforms the :class:`Suite`() call so that it always returns the instance of a
    concrete subclass of Suite. There should only be one concrete subclass of Suite."""
    instance = None
    def __init__(cls, name, bases, nmspc):
        super(SuiteMetaclass, cls).__init__(name, bases, nmspc)
        cls.name = utils.convert_camelcase(name)
        if not hasattr(cls, 'registry'):
            cls.registry = set()
        cls.registry.add(cls)
        cls.registry -= set(bases) # Remove base classes

    def __call__(cls, *args, **kwargs):
        if len(cls.registry) > 1:
            raise SuiteException("There can only be one final environment class")

        if not Suite.instance:
            c = next(iter(cls.registry))
            Suite.instance = super(SuiteMetaclass, c).__call__(*args, **kwargs)
        return Suite.instance


class Suite(Mapping, metaclass=SuiteMetaclass):
    """This is the "environment" for Sondra. Similar to a `settings.py` file in Django, it defines the
    environment in which all :class:`Application`s exist.

    The Suite is also a mapping type, and it should be used to access or enumerate all the :class:`Application` objects
    that are registered.

    Attributes:
        applications (dict): A mapping from application name to Application objects. Suite itself implements a mapping
            protocol and this is its backend.
        async (dict): (Unsupported)
        base_url (str): The base URL for the API. The Suite will be mounted off of here.
        base_url_scheme (str): http or https, automatically set.
        base_url_netloc (str): automatically set hostname of the suite.
        connection_config (dict): For each key in connections setup keyword args to be passed to `rethinkdb.connect()`
        connections (dict): RethinkDB connections for each key in ``connection_config``
        docstring_processor_name (str): Any member of DOCSTRING_PROCESSORS: ``preformatted``, ``rst``, ``markdown``,
            ``google``, or ``numpy``.
        docstring_processor (callable): A ``lambda (str)`` that returns HTML for a docstring.
        logging (dict): A dict-config for logging.
        log (logging.Logger): A logger object configured with the above dictconfig.
        schema (dict): The schema of a suite is a dict where the keys are the names of :class:`Application` objects
            registered to the suite. The values are the schemas of the named app.  See :class:`Application` for more
            details on application schemas.
    """
    applications = {}
    async = False
    base_url = "http://localhost:8000"
    logging = None
    docstring_processor_name = 'preformatted'
    connection_config = {
        'default': {}
    }

    def __init__(self):
        if self.logging:
            logging.config.dictConfig(self.logging)
        else:
            logging.basicConfig()

        self.log = logging  # use root logger for the environment

        self.connections = {name: r.connect(**kwargs) for name, kwargs in self.connection_config.items()}
        for name in self.connections:
            self.log.warning("Connection established to '{0}'".format(name))

        p_base_url = urlparse(self.base_url)
        self.base_url_scheme = p_base_url.scheme
        self.base_url_netloc = p_base_url.netloc
        self.base_url_path = p_base_url.path
        self.log.warning("Suite base url is: '{0}".format(self.base_url))

        self.docstring_processor = DOCSTRING_PROCESSORS[self.docstring_processor_name]
        self.log.info('Docstring processor is {0}')

    def register_application(self, app):
        """This is called automatically whenever an Application object is constructed."""
        if app.slug in self.applications:
            self.log.error("Tried to register application '{0}' more than once.".format(app.slug))
            raise SuiteException("Tried to register multiple applications with the same name.")

        self.applications[app.slug] = app
        self.log.info('Registered application {0} to {1}'.format(app.__class__.__name__, app.url))


    def __getitem__(self, item):
        """Application objects are indexed by "slug." Every Application object registered has its name slugified.

        This means that if your app was called `MyCoolApp`, its registered name would be `my-cool-app`. This key is
        used whether you are accessing the application via URL or locally via Python.  For example, the following
        both produce the same result::

            URL (yields schema as application/json):

                http://localhost:5000/api/my-cool-app;schema

            Python (yields schema as a dict):

                suite = Suite()
                suite['my-cool-app'].schema
        """
        return self.applications[item]

    def __len__(self):
        return len(self.applications)

    def __iter__(self):
        return iter(self.applications)

    def __contains__(self, item):
        return item in self.applications

    def lookup(self, url):
        if not url.startswith(self.base_url):
            return None
        else:
            return Reference(Suite(), url).value

    def lookup_document(self, url):
        if not url.startswith(self.base_url):
            return None
        else:
            return Reference(Suite(), url).get_document()

    @property
    def schema(self):
        ret = {
            "name": self.base_url,
            "description": self.__doc__,
            "definitions": copy(BASIC_TYPES)
        }

        for app in self.applications.values():
            ret['definitions'][app.name] = app.schema

        return ret



class CollectionMetaclass(ABCMeta):
    def __init__(cls, name, bases, nmspc):
        super(CollectionMetaclass, cls).__init__(name, bases, nmspc)
        cls.name = utils.convert_camelcase(cls.__name__)
        cls.slug = utils.camelcase_slugify(cls.__name__)

        cls.schema = deepcopy(cls.document_class.schema)
        if 'description' not in cls.schema:
            cls.schema['description'] = cls.__doc__ or "No description provided"
        if 'id' in cls.schema['properties']:
            raise CollectionException('Document schema should not have an "id" property')
        if not cls.primary_key:
            cls.schema['properties']['id'] = {"type": "string"}

        _validator.check_schema(cls.schema)

        if not hasattr(cls, 'application') or cls.application is None:
            raise CollectionException("{0} declared without application".format(name))
        else:
            cls.application.register_collection(cls)


class ApplicationMetaclass(ABCMeta):
    def __init__(cls, name, bases, nmspc):
        super(ApplicationMetaclass, cls).__init__(name, bases, nmspc)
        cls._collection_registry = {}

    def __iter__(cls):
        return (i for i in cls._collection_registry.items())

    def register_collection(cls, collection_class):
        if collection_class.name not in cls._collection_registry:
            cls._collection_registry[collection_class.slug] = collection_class
        else:
            raise ApplicationException("{0} registered twice".format(collection_class.slug))

    def __call__(cls, *args, **kwargs):
        instance = super(ApplicationMetaclass, cls).__call__(*args, **kwargs)
        Suite().register_application(instance)
        return instance


class Application(Mapping, metaclass=ApplicationMetaclass):
    """A reusable group of :class:`Collections` and optional top-level exposed functionality.

    An Application can contain any number of :class:`Collection`s.

    """
    db = 'default'
    connection = 'default'
    slug = None
    collections = None
    anonymous_reads = True

    def __init__(self, name=None):
        self.env = Suite()
        self.name = name or self.__class__.__name__
        self.slug = utils.camelcase_slugify(self.name)
        self.db = utils.convert_camelcase(self.name)
        self.connection = Suite().connections[self.connection]
        self.collections = {}
        self.url = '/'.join((self.env.base_url, self.slug))
        self.log = logging.getLogger(self.name)
        self.application = self

        self.before_init()
        for name, collection_class in self.__class__:
            self.collections[name] = collection_class(self)
        self.after_init()

    def __len__(self):
        return len(self.collections)

    def __getitem__(self, item):
        return self.collections[item]

    def __iter__(self):
        return iter(self.collections)

    def __contains__(self, item):
        return item in self.collections

    def create_tables(self, *args, **kwargs):
        for collection_class in self.collections.values():
            try:
                collection_class.create_table(*args, **kwargs)
            except:
                pass

    def drop_tables(self, *args, **kwargs):
        for collection_class in self.collections.values():
            try:
                collection_class.drop_table(*args, **kwargs)
            except:
                pass

    def create_database(self):
        try:
            r.db_create(self.db).run(self.connection)
        except r.ReqlError as e:
            self.log.info(e.message)

    def drop_database(self):
        r.db_drop(self.db).run(self.connection)

    def after_init(self):
        pass

    def before_init(self):
        pass

    @property
    def schema(self):
        ret = {
            "id": self.url + ";schema",
            "description": self.__doc__ or "No description provided",
            "definitions": copy(BASIC_TYPES)
        }

        for name, coll in self.collections.items():
            ret['definitions'][name] = coll.schema

        return ret


class Document(MutableMapping):
    schema = {
        "type": "object",
        "properties": {}
    }

    def __init__(self, obj, collection=None, parent=None):
        self.collection = collection
        if not self.collection and (parent and parent.collection):
            self.collection = parent.collection

        self.parent = parent

        self.url = None
        if self.collection.primary_key in obj:
            self.url = '/'.join((self.collection.url, obj[self.collection.primary_key]))

        self._referenced = True
        self.obj = {}
        for k, v in obj.items():
            self[k] = v

    @property
    def application(self):
        return self.collection.application

    @property
    def id(self):
        return self.obj.get(self.collection.primary_key, None)

    @id.setter
    def id(self, v):
        self.obj[self.collection.primary_key] = v
        self.url = '/'.join((self.collection.url, v))

    @property
    def name(self):
        return self.id

    @property
    def slug(self):
        return self.id

    def __len__(self):
        return len(self.obj)

    def __eq__(self, other):
        return self.id and (self.id == other.id)

    def __getitem__(self, key):
        return self.obj[key]

    def __setitem__(self, key, value):
        if isinstance(value, Document):
            value.parent = self
            self.referenced = False
        self.obj[key] = value

    def __delitem__(self, key):
        del self.obj[key]

    def __iter__(self):
        return iter(self.obj)

    def json(self, *args, **kwargs):
        if not self._referenced:
            self.reference()
        return json.dumps(self.obj, *args, **kwargs)

    def save(self, *args, **kwargs):
        return self.collection.save(self.obj, *args, **kwargs)

    def delete(self, **kwargs):
        return  self.collection.delete(self.id, **kwargs)

    def dereference(self):
        self.obj = documents(self.obj)
        self._referenced = False
        return self

    def reference(self):
        self.obj = references(self.obj)
        self._referenced = True
        return self

    def validate(self):
        jsonschema.validate(self.obj, self.schema)


class Collection(MutableMapping, metaclass=CollectionMetaclass):
    name = None
    slug = None
    schema = None
    application = Application
    document_class = Document
    primary_key = "id"
    private = False
    specials = {}
    indexes = []
    relations = []
    anonymous_reads = True

    @property
    def table(self):
        return r.db(self.application.db).table(self.name)

    def __init__(self, application):
        self.application = application
        self.url = '/'.join((self.application.url, self.slug))
        self.schema['id'] = self.url + ";schema"
        self.log = logging.getLogger(self.application.name + "." + self.name)
        self.after_init()

    def create_table(self, *args, **kwargs):
        self.before_table_create()

        try:
            r.db(self.application.db)\
                .table_create(self.name, primary_key=self.primary_key, *args, **kwargs)\
                .run(self.application.connection)
        except r.ReqlError as e:
            self.log.info('Table {0}.{1} already exists.'.format(self.application.db, self.name))

        for index in self.indexes:
            if isinstance(index, tuple):
                index, index_function = index
            else:
                index_function = None

            if self.schema['properties'][index].get('type', None) == 'array':
                multi = True
            else:
                multi = False

            if index in self.specials and self.specials[index].is_geometry:
                geo = True
            else:
                geo = False

            try:
                if index_function:
                    self.table.index_create(index, index_function, multi=multi, geo=geo).run(self.application.connection)
                else:
                    self.table.index_create(index, multi=multi, geo=geo).run(self.application.connection)
            except r.ReqlError as e:
                self.log.info('Index on table {0}.{1} already exists.'.format(self.application.db, self.name))

        self.after_table_create()

    def drop_table(self):
        self.before_table_drop()
        ret = r.db(self.application.db).table_drop(self.name).run(self.application.connection)
        self.log.info('Dropped table {0}.{1}'.format(self.application.db, self.name))
        self.after_table_drop()
        return ret

    def _to_python_repr(self, doc):
        for property, special in self.specials.items():
            if property in doc:
                doc[property] = special.to_python_repr(doc[property])

    def _to_json_repr(self, doc):
        for property, special in self.specials.items():
            if property in doc:
                doc[property] = special.to_json_repr(doc[property])

    def _to_rql_repr(self, doc):
        for property, special in self.specials.items():
            if property in doc:
                doc[property] = special.to_rql_repr(doc[property])

    def __getitem__(self, key):
        doc = self.table.get(key).run(self.application.connection)
        if doc:
            self._to_python_repr(doc)
            return self.document_class(doc, collection=self)
        else:
            raise KeyError('{0} not found in {1}'.format(key, self.url))

    def __setitem__(self, key, value):
        return self.save(value, conflict='replace').run(self.application.connection)

    def __delitem__(self, key):
        self.before_delete()
        self.table.get(key).delete().run(self.application.connection)
        self.after_delete()

    def __iter__(self):
        for doc in self.table.run(self.application.connection):
            self._to_python_repr(doc)
            yield doc

    def __contains__(self, item):
        doc = self.table.get(item).run(self.application.connection)
        return doc is not None

    def __len__(self):
        return self.table.count().run(self.application.connection)

    def q(self, query):
        for doc in query.run(self.application.connection):
            self._to_python_repr(doc)
            yield self.document_class(doc, collection=self)

    def doc(self, kwargs):
        return self.document_class(kwargs, collection=self)

    def create(self,kwargs):
        doc = self.document_class(kwargs, collection=self)
        ret = self.save(doc, conflict="error")
        if 'generated_keys' in ret:
            doc.id = ret['generated_keys'][0]
        return doc

    def validator(self, value):
        return True

    def before_validation(self):
        pass

    def before_save(self):
        pass

    def after_save(self):
        pass

    def before_delete(self):
        pass

    def after_delete(self):
        pass

    def before_table_create(self):
        pass

    def after_table_create(self):
        pass

    def before_table_drop(self):
        pass

    def after_table_drop(self):
        pass

    def after_init(self):
        pass

    def delete(self, docs, **kwargs):
        if not isinstance(docs, list):
            docs = [docs]

        values = [v.id if isinstance(v, Document) else v for v in docs]
        return self.table.get_all(*values).delete(**kwargs).run(self.application.connection)

    def save(self, docs, **kwargs):
        if not isinstance(docs, list):
            docs = [docs]

        values = []
        self.before_save()
        for value in docs:
            if isinstance(value, Document):
                value = copy(value.obj)

            self.before_validation()
            value = references(value)  # get rid of Document objects and turn them into URLs
            self._to_json_repr(value)
            jsonschema.validate(value, self.schema)
            self.validator(value)

            self._to_rql_repr(value)
            values.append(value)

        ret = self.table.insert(values, **kwargs).run(self.application.connection)
        self.after_save()
        return ret



