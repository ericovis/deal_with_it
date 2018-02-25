import json
import base64
from flask import Flask, request, Response
from deal_with_it import ImageProcessor


app = Flask(__name__)


def response(body, status=200, mimetype="application/json"):
    res = {
        'body': body,
        'status': status
    }
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
            proc = ImageProcessor(args['img_url'])
            img = proc.deal_with_it()
            media_data = base64.b64encode(img).decode('utf8').replace("'", '')
            return response({'message': 'Deal with it!',
                             'media_data': media_data})
        else:
            return response('I need an image URL to work with.', status=400)
    return response('I only understand JSON', status=400)


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
