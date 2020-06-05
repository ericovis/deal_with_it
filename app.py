import json
import os
import base64
from flask import Flask, request, render_template, Response
from processors import ImageProcessor

app = Flask(__name__)

def json_response(message, status=200):
    body = dict(message=message, status=status)
    return Response(
                    response=json.dumps(body),
                    status=status,
                    mimetype="application/json"
                   )


def image_response(image, status=200):
    message = json.dumps({"image": image})
    return json_response(message, status)


@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')


@app.route('/api', methods=['POST'])
def api():
    if request.is_json:
        data = request.get_json()
    
        if data.get('image', False):
            proc = ImageProcessor(image=data['image'])
            img = proc.get_base64_array()            
            return image_response(img)
        elif data.get('url', False):
            proc = ImageProcessor(url=data['url'])
            img = proc.get_base64_array()            
            return image_response(img)
        else:
            return json_response("Missing the 'img' field on request body", status=400)
    
    return json_response('I only understand JSON', status=400)


@app.errorhandler(500)
def error(e):
    return json_response("Something went wrong", status=500)


if __name__ == '__main__':
    if os.environ.get('PORT'):
        app.run(host='0.0.0.0', debug=False, port=int(os.environ.get('PORT')))
    else:
        app.run(host='0.0.0.0', debug=True, port=5000)
