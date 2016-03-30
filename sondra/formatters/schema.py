import json
from sondra.utils import natural_order
from sondra.expose import method_schema

class Schema(object):
    """
    Returns the schema of the target reference.

    Optional args:

    * indent (int) - If specified, the formatter pretty prints the JSON for human reading with indented lines.
    """

    name = 'schema'

    def __call__(self, reference, result, **kwargs):
        if 'indent' in kwargs:
            kwargs['indent'] = int(kwargs['indent'])

        if 'method' in reference.kind:
            ordered_schema = natural_order(method_schema(*reference.value))
            return 'application/json', json.dumps(ordered_schema, **kwargs)
        else:
            ordered_schema = natural_order(reference.value.schema)
            return 'application/json', json.dumps(ordered_schema, **kwargs)
