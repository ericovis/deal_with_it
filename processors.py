import re
import base64
from urllib import request
from io import BytesIO
from PIL import Image
import face_recognition as fr
import image_to_numpy
from math import atan2, degrees



class DealWithItProcessor(object):
    offset = 415/1024
    img_format = 'PNG'
    resample = Image.BILINEAR
    glasses = './static/img/glasses.png'    

    def __init__(self, image=None, url=None):
        self.url = url
        self.image = image        
        self.__validated = None     
        self.found_faces = False  
        self.output = None

    def is_valid(self):
        if self.__validated is not None:
            return self.__validated
        
        try:
            if self.url:
                req = request.urlopen(self.url)
                self.image = image_to_numpy.load_image_file(BytesIO(req.read()))
            elif self.image:
                base64_data = re.sub('^data:image/.+;base64,', '', self.image)
                byte_data = base64.b64decode(base64_data)
                image_data = BytesIO(byte_data) 
                self.image = image_to_numpy.load_image_file(image_data)
            self.__validated = True
        except Exception as e:
            self.__validated = False
        
        return self.__validated

    def __get_glasses(self, new_width, angle=0, increase=0.3,):
        img = Image.open(self.glasses).convert("RGBA")
        if angle != 0:
            img = img.rotate(angle, expand=True, resample=self.resample)
        width, height = img.size
        new_width = new_width + int(float(new_width) * increase)
        new_height = int((float(height)*float((new_width/float(width)))))
        img = img.resize((new_width, new_height))
        return img

    def __get_final_position(self, img_size, left_eye, angle):
        x = left_eye[0] - int(float(img_size[0]) * self.offset)
        y = left_eye[1] - int(img_size[1]/2)
        return (x, y)

    def __get_angle(self, left_eye, right_eye):
        xDiff = right_eye[0] - left_eye[0]
        yDiff = right_eye[1] - left_eye[1]
        angle = int(degrees(atan2(yDiff, xDiff)))
        return -angle

    def process(self):
        if not self.__validated:
            raise Exception('Input image is not validated')
    
        self.output = Image.fromarray(self.image)
        face_locations = fr.face_locations(self.image)
        
        if face_locations:
            self.found_faces = True
        
        for face in face_locations:
            landmarks = fr.face_landmarks(self.image, face_locations=[face])
            left_eye = landmarks[0]['left_eye'][0]
            right_eye = landmarks[0]['right_eye'][3]
            angle = self.__get_angle(left_eye, right_eye)
            glasses = self.__get_glasses(face[1]-face[3], angle=angle)
            position = self.__get_final_position(glasses.size, left_eye, angle)
            self.output.paste(glasses, position, mask=glasses)
        
        return self.output

    def get_base64_image(self):            
        if self.output is None:
            self.process()
        
        arr = BytesIO()
        self.output.save(arr, format=self.img_format)
        arr = arr.getvalue()
        img = base64.b64encode(arr).decode('utf8').replace("'", '')
        return f'data:image/png;base64,{img}'
