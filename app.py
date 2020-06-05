import json
import base64
from flask import Flask, request, Response
from deal_with_it import ImageProcessor

app = Flask(__name__)



def response(message, status=200, mimetype="application/json"):
    res = {
        'status': status
    }
    if type(message) is str:
        res['message'] = message
    elif type(message) is dict:
        res['body'] = message
    return Response(
                    response=json.dumps(res),
                    status=status,
                    mimetype=mimetype
                   )


@app.route('/', methods=['POST'])
def route():
    if request.is_json:
        args = request.get_json()
        if args.get('img_url', False):
            app.logger.info("Processing image")
            proc = ImageProcessor(args['img_url'])
            img = proc.get_base64_array()
            app.logger.info("Image was processed")
            return response({'media_data': img})
        else:
            app.logger.info("Missing the 'img_url' field")
            return response('I need an image URL to work with.', status=400)
    app.logger.info('Request is not JSON encoded')
    return response('I only understand JSON', status=400)


@app.errorhandler(500)
def error(e):
    return response("Something went wrong", status=500)


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
