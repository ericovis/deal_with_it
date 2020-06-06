import json
import os
import base64
from flask import Flask, request, render_template, Response
from processors import DealWithItProcessor

app = Flask(__name__)

DEFAULT_CACHE = 60 * 60 * 2 


def json_response(body, status=200):
    return Response(
        response=json.dumps(body),
        status=status,
        mimetype="application/json"
    )


def json_msg_response(message, status=200):
    body = dict(message=message, status=status)
    return json_response(body, status)


def image_response(image, status=200):
    body = json.dumps({"image": image})
    return json_response(body, status)


@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')


@app.route('/api', methods=['POST'])
def api():
    if request.is_json:  
        data = request.get_json()
        if not data.get('image', False) and not data.get('url', False):
            return json_msg_response("Missing the required fields on request body", status=400)

        proc = DealWithItProcessor(**data)

        if not proc.is_valid():           
            return json_msg_response("The provided image or url is invalid", 400)     
        
        proc.process()
        if not proc.found_faces:
            return json_msg_response("No faces were found on this image", 400)     

        return image_response(proc.get_base64_image())                  
    
    return json_msg_response('I only understand JSON', status=400)


@app.errorhandler(500)
def error(e):
    return json_response("Something went wrong", status=500)


app.config['SEND_FILE_MAX_AGE_DEFAULT'] = DEFAULT_CACHE


@app.after_request
def add_header(response):
    response.cache_control.max_age = DEFAULT_CACHE
    return response



if __name__ == '__main__':
    debug = bool(os.environ.get('DEBUG', False))
    app.run(
        host='0.0.0.0',
        debug=debug,
        port=int(os.environ.get('PORT', 5000))
    )
