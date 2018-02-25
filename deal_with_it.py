from urllib import request
from io import BytesIO
from PIL import Image
import face_recognition as fr
from math import atan2, degrees


class ImageProcessor(object):
    offset = 415/1024
    resample = Image.BILINEAR
    glasses_img_path = './images/glasses.png'
    tmp_dir = './tmp'

    def __init__(self, img_url):
        req = request.urlopen(img_url)
        self.img_url = img_url
        self.img_arr = fr.load_image_file(BytesIO(req.read()))
        self.output = Image.fromarray(self.img_arr)
        self.output_arr = BytesIO()

    def get_glasses(self, new_width, angle=0, increase=0.3,):
        img = Image.open(self.glasses_img_path)
        img = img.convert("RGBA")
        width, height = img.size
        new_width = new_width + int(float(new_width) * increase)
        new_height = int((float(height)*float((new_width/float(width)))))
        if angle != 0:
            img = img.rotate(angle, expand=False, resample=self.resample)
        return img.resize((new_width, new_height))

    def get_final_position(self, img_size, left_eye):
        x = left_eye[0] - int(float(img_size[0]) * self.offset)
        y = left_eye[1] - int(img_size[1]/2)
        return (x, y)

    def get_angle(self, left_eye, right_eye):
        xDiff = right_eye[0] - left_eye[0]
        yDiff = right_eye[1] - left_eye[1]
        return -degrees(atan2(yDiff, xDiff))

    def deal_with_it(self):
        face_locations = fr.face_locations(self.img_arr)
        for face in face_locations:
            landmarks = fr.face_landmarks(self.img_arr, face_locations=[face])
            left_eye = landmarks[0]['left_eye'][0]
            right_eye = landmarks[0]['right_eye'][3]
            angle = self.get_angle(left_eye, right_eye)
            glasses = self.get_glasses(face[1]-face[3])
            position = self.get_final_position(glasses.size, left_eye)
            self.output.paste(glasses, position, mask=glasses)
            self.output.save(self.output_arr, format='PNG')
        return self.output_arr.getvalue()
