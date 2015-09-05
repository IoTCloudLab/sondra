from flask import request, Blueprint, abort, current_app, Response
import json

from .api import APIRequest

api_tree = Blueprint('api', __name__)

@api_tree.route('/schema')
def suite_schema():
    return Response(
        json.dumps(current_app.suite.schema, indent=4),
        status=200,
        mimetype='application/json'
    )

@api_tree.route('/<path:path>', methods=['GET','POST','PUT','PATCH', 'DELETE'])
def api_request(path):
    args = dict(request.args)
    if 'q' not in args:
        if request.form:
            args['q'] = [json.dumps({k: v for k, v in request.form})]

    r = APIRequest(
        current_app.suite,
        request.headers,
        request.data,
        request.method,
        None,
        current_app.suite.base_url + '/' + path,
        args,
        request.files
    )

    mimetype, response = r()
    return Response(
        response=response,
        status=200,
        mimetype=mimetype)