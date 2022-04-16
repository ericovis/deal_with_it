import os
import face_recognition as fr

from PIL import Image
from math import atan2, degrees
from src.processors.base import BaseProcessor, ImageModel


class DealWithItProcessor(BaseProcessor):
    offset = 415/1024
    resample = Image.BILINEAR
    

    def __init__(self, image: ImageModel, static_dir: str):
        super().__init__(image)
        self._glasses_path = os.path.join(static_dir, 'img/glasses.png')    


    def _get_glasses(self, new_width, angle=0, increase=0.3,):
        img = Image.open(self._glasses_path).convert("RGBA")
        if angle != 0:
            img = img.rotate(angle, expand=True, resample=self.resample)
        width, height = img.size
        new_width = new_width + int(float(new_width) * increase)
        new_height = int((float(height)*float((new_width/float(width)))))
        img = img.resize((new_width, new_height))
        return img

    def _get_final_position(self, img_size, left_eye):
        x = left_eye[0] - int(float(img_size[0]) * self.offset)
        y = left_eye[1] - int(img_size[1]/2)
        return (x, y)

    def _get_angle(self, left_eye, right_eye):
        xDiff = right_eye[0] - left_eye[0]
        yDiff = right_eye[1] - left_eye[1]
        angle = int(degrees(atan2(yDiff, xDiff)))
        return -angle

    def call(self) -> None:   
        face_locations = fr.face_locations(self.image.data)       
        if face_locations:
            self.output = Image.fromarray(self.image.data)
            for face in face_locations:
                landmarks = fr.face_landmarks(self.image.data, face_locations=[face])
                left_eye = landmarks[0]['left_eye'][0]
                right_eye = landmarks[0]['right_eye'][3]
                angle = self._get_angle(left_eye, right_eye)
                glasses = self._get_glasses(face[1]-face[3], angle=angle)
                position = self._get_final_position(glasses.size, left_eye)
                self.output.paste(glasses, position, mask=glasses)
