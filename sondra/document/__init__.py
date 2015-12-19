"""Core data document types.
"""
import json
import logging

from abc import ABCMeta
from collections.abc import MutableMapping
from functools import partial
import jsonschema
from slugify import slugify

try:
    from shapely.geometry import mapping, shape
    from shapely.geometry.base import BaseGeometry
except:
    logging.warning("Shapely not imported. Geometry objects will not be supported directly.")

from sondra import utils, help
from sondra.utils import mapjson, split_camelcase
from sondra.ref import Reference

__all__ = (
    "Document",
    "DocumentMetaclass"
)


def _reference(v):
    if isinstance(v, Document):
        if not v.id:
            v.save()
        return v.url
    else:
        return v


class DocumentMetaclass(ABCMeta):
    """
    The metaclass for all documents merges definitions and schema into a single schema attribute and makes sure that
    exposed methods are catalogued.
    """
    def __new__(mcs, name, bases, attrs):
        definitions = {}
        schema = attrs.get('schema', {"type": "object", "properties": {}})

        for base in bases:  # make sure this class inherits definitions and schemas
            if hasattr(base, "definitions") and base.definitions:
                definitions.update(base.definitions)
            if hasattr(base, "collection"):
                if "allOf" not in schema:
                    schema["allOf"] = []
                schema['allOf'].append({"$ref": base.collection.schema_url})

        if "definitions" in attrs:
            attrs['definitions'].update(definitions)
        else:
            attrs['definitions'] = definitions

        if 'title' not in attrs or (attrs['title'] is None):
            if 'title' in schema:
                attrs['title'] = schema['title']
            else:
                attrs['title'] = split_camelcase(name)

        attrs['schema'] = schema
        attrs['schema']['title'] = attrs['title']

        return super().__new__(mcs, name, bases, attrs)

    def __init__(cls, name, bases, nmspc):
        super(DocumentMetaclass, cls).__init__(name, bases, nmspc)
        cls.exposed_methods = {}

        for base in bases:
            if hasattr(base, 'exposed_methods'):
                cls.exposed_methods.update(base.exposed_methods)

        for name, method in (n for n in nmspc.items() if hasattr(n[1], 'exposed')):
                cls.exposed_methods[name] = method

        if 'description' not in cls.schema and cls.__doc__:
            cls.schema['description'] = cls.__doc__

        cls.schema['methods'] = [m.slug for m in cls.exposed_methods.values()]
        cls.schema['definitions'] = nmspc.get('definitions', {})
        cls.schema['template'] = nmspc.get('template','{id}')

        cls.defaults = {k: cls.schema['properties'][k]['default']
                        for k in cls.schema['properties']
                        if 'default' in cls.schema['properties'][k]}


class Document(MutableMapping, metaclass=DocumentMetaclass):
    """
    The base type of an individual RethinkDB record.

    Each record is an instance of exactly one document class. To combine schemas and object definitions, you can use
    Python inheritance normally.  Inherit from multiple Document classes to create one Document class whose schema and
    definitions are combined by reference.

    Most Document subclasses will define at the very least a docstring,

    Attributes:
        collection (sondra.collection.Collection): The collection this document belongs to. FIXME could also use URL.
        defaults (dict): The list of default values for this document's properties.
        title (str): The title of the document schema. Defaults to the case-split name of the class.
        template (string): A template string for formatting documents for rendering.  Can be markdown.
        schema (dict): A JSON-serializable object that is the JSON schema of the document.
        definitions (dict): A JSON-serializable object that holds the schemas of all referenced object subtypes.
        exposed_methods (list): A list of method slugs of all the exposed methods in the document.
    """
    template = "{id}"
    processors = []

    def __init__(self, obj, collection=None, from_db=False):
        self.collection = collection
        self._saved = from_db

        if self.collection:
            self.schema = self.collection.schema  # this means it's only calculated once. helpful.
        else:
            self.schema = mapjson(lambda x: x(context=self) if callable(x) else x, self.schema)  # turn URL references into URLs

        self._url = None
        if self.collection.primary_key in obj:
            self._url = '/'.join((self.collection.url, _reference(obj[self.collection.primary_key])))

        self._referenced = True
        self.obj = {}
        if obj:
            for k, v in obj.items():
                self[k] = v

    @property
    def application(self):
        """The application instance this document's collection is attached to."""
        return self.collection.application

    @property
    def suite(self):
        """The suite instance this document's application is attached to."""
        return self.application.suite

    @property
    def id(self):
        """The value of the primary key field. None if the value has not yet been saved."""
        if self._saved:
            return self.obj[self.collection.primary_key]
        else:
            return None

    @id.setter
    def id(self, v):
        self.obj[self.collection.primary_key] = v
        self._url = '/'.join((self.collection.url, v))

    @property
    def name(self):
        return self.id or "<unsaved>"

    @property
    def url(self):
        if self._url:
            return self._url
        elif self.collection:
            return self.collection.url + "/" + self.slug
        else:
            return self.slug

    @property
    def schema_url(self):
        return self.url + ";schema"

    @property
    def slug(self):
        """Included for symmetry with application and collection, the same as 'id'."""
        return self.id   # or self.UNSAVED

    def __len__(self):
        """The number of keys in the object"""
        return len(self.obj)

    def __eq__(self, other):
        """True if and only if the primary keys are the same"""
        return self.id and (self.id == other.id)

    def __getitem__(self, key):
        """Return either the value of the property or the default value of the property if the real value is undefined"""
        if key in self.obj:
            return self.obj[key]
        elif key in self.defaults:
            return self.defaults[key]
        else:
            raise KeyError(key)

    def fetch(self, key):
        """Return the value of the property interpreting it as a reference to another document"""
        if key in self.obj:
            if isinstance(self.obj[key], list):
                return [self.suite.from_doc(ref) for ref in self.obj[key]]
            elif isinstance(self.obj[key], dict):
                return {k: self.suite.from_doc(ref) for k, ref in self.obj[key].items()}
            if self.obj[key] is not None:
                return Reference(self.suite, self.obj[key]).value
            else:
                return None
        else:
            raise KeyError(key)

    def __setitem__(self, key, value):
        """Set the value of the property, saving it if it is an unsaved Document instance"""
        value = _reference(value)
        if isinstance(value, list) or isinstance(value, dict):
            value = mapjson(_reference, value)

        self.obj[key] = value
        for p in self.processors:
            if p.is_necessary(key):
                p.run(self.obj)

    def __delitem__(self, key):
        del self.obj[key]
        if self.collection:
            for p in self.collection.processors:
                if p.is_necessary(key):
                    p.run(self.obj)

    def __iter__(self):
        return iter(self.obj)

    def help(self, out=None, initial_heading_level=0):
        """Return full reStructuredText help for this class"""
        builder = help.SchemaHelpBuilder(self.schema, self.url, out=out, initial_heading_level=initial_heading_level)
        builder.begin_subheading(self.name)
        builder.begin_list()
        builder.define("Collection", self.collection.url + ';help')
        builder.define("Schema URL", self.schema_url)
        builder.define("JSON URL", self.url)
        builder.end_list()
        builder.end_subheading()
        builder.build()
        if self.exposed_methods:
            builder.begin_subheading("Methods")
            for name, method in self.exposed_methods.items():
                new_builder = help.SchemaHelpBuilder(method.schema(getattr(self, method.__name__)), initial_heading_level=builder._heading_level)
                new_builder.build()
                builder.line(new_builder.rst)

        return builder.rst

    def json(self, *args, **kwargs):
        return json.dumps(self.obj, *args, **kwargs)

    def save(self, *args, **kwargs):
        return self.collection.save(self.obj, *args, **kwargs)

    def delete(self, **kwargs):
        return  self.collection.delete(self.id, **kwargs)

    def validate(self):
        jsonschema.validate(self.obj, self.schema)


class DocumentProcessor(object):
    def is_necessary(self, changed_props):
        """Override this method to determine whether the processor should run."""
        return False

    def run(self, document):
        """Override this method to post-process a document after it has changed."""
        return document


class SlugPropertyProcessor(DocumentProcessor):
    def __init__(self, source_prop, dest_prop='slug'):
        self.dest_prop = dest_prop
        self.source_prop = source_prop

    def is_necessary(self, changed_props):
        return self.source_prop in changed_props

    def run(self, document):
        document[self.dest_prop] = slugify(document[self.source_prop])