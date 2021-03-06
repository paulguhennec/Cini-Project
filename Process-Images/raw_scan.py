import os.path

import numpy as np
import unwarp
import utils
from image import ExtractedImage
from base import *
from cardboard import RectoCardboard, VersoCardboard
import shared
import cv2
from scipy.misc import imresize
from PIL import Image
import matplotlib.pyplot as plt
from scipy.ndimage.interpolation import map_coordinates
from skimage.transform import warp_coords
from typing import Union


class RawScan:
    def __init__(self, document_info: DocumentInfo, base_path: str):
        """
        :param document_info:
        :param base_path: Base path of the files to be examined (ex : '/mnt/Cini/1A/1A_37')
        :return:
        """
        self.document_info = document_info
        self.cropped_cardboard = None

        if self.document_info.side == 'recto':
            self.image_path = base_path + shared.RECTO_SUBSTRING_JPG
        else:
            self.image_path = base_path + shared.VERSO_SUBSTRING_JPG

        # Checks
        assert os.path.exists(self.image_path)

        # Loads the image

        self.raw_scan = utils.load_jpg_file_to_image(self.image_path)

        self.output_prediction = shared.PREDICTION_CARDBOARD_DEFAULT_FILENAME

        if self.document_info.side == 'recto':
            self.output_filename = shared.RECTO_CARDBOARD_DEFAULT_FILENAME
        else:
            self.output_filename = shared.VERSO_CARDBOARD_DEFAULT_FILENAME

    def crop_cardboard(self, model, do_unwarp=False):
        with CatchTime('Resizing + Predicion'):
            # Performs the crop
            full_size_image = self.raw_scan
            original_h, original_w = full_size_image.shape[:2]
            #self.resized_raw_scan = cv2.resize(full_size_image, (target_w, target_h))

            prediction = model.predict(self.resized_raw_scan[None, :, :, :], prediction_key='labels')[0]
            self.prediction = prediction
            self.prediction_scale = prediction.shape[0]/original_h

        # class 0 -> cardboard
        # class 1 -> background
        # class 2 -> photograph

        cardboard_prediction = unwarp.get_cleaned_prediction((prediction == 0).astype(np.uint8))
        _, contours, hierarchy = cv2.findContours(cardboard_prediction, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        cardboard_contour = np.concatenate(contours)  # contours[np.argmax([cv2.contourArea(c) for c in contours])]
        cardboard_rectangle = cv2.minAreaRect(cardboard_contour)
        # If extracted cardboard too small compared to scan size, get cardboard+image prediction
        if cv2.contourArea(cv2.boxPoints(cardboard_rectangle)) < 0.20 * cardboard_prediction.size:
            cardboard_prediction = unwarp.get_cleaned_prediction(
                ((prediction == 0) | (prediction == 2)).astype(np.uint8))
            _, contours, hierarchy = cv2.findContours(cardboard_prediction, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
            cardboard_contour = np.concatenate(contours)  # contours[np.argmax([cv2.contourArea(c) for c in contours])]
            cardboard_rectangle = cv2.minAreaRect(cardboard_contour)

        image_prediction = (prediction == 2).astype(np.uint8)
        # Force the image prediction to be inside the extracted cardboard
        mask = np.zeros_like(image_prediction)
        cv2.fillConvexPoly(mask, cv2.boxPoints(cardboard_rectangle).astype(np.int32), 1)
        image_prediction = mask * image_prediction
        eroded_mask = cv2.erode(mask, np.ones((20, 20)))
        image_prediction = image_prediction | (~cardboard_prediction & eroded_mask)

        image_prediction = unwarp.get_cleaned_prediction(image_prediction)
        _, contours, hierarchy = cv2.findContours(image_prediction, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)
        # Take the biggest contour or two biggest if similar size (two images in the page)
        image_contour = contours[0] if len(contours) == 1 or (
                    cv2.contourArea(contours[0]) > 0.5 * cv2.contourArea(contours[1])) \
            else np.concatenate(contours[0:2])
        image_rectangle = cv2.minAreaRect(image_contour)

        self.cardboard_rectangle = cardboard_rectangle
        self.image_rectangle = image_rectangle

        if do_unwarp:
            self.p, self.center_x, self.center_y = unwarp.uwrap(self.prediction)
            self.prediction = map_coordinates(self.prediction, warp_coords(self.transform, self.prediction.shape),
                                              order=1, prefilter=False)
            self.warped_image = map_coordinates(full_size_image, warp_coords(self.transform, full_size_image.shape),
                                                order=1, prefilter=False)
        else:
            self.warped_image = full_size_image

        self.cropped_cardboard = self.extract_minAreaRect(self.warped_image, self.cardboard_rectangle,
                                                          scale=1/self.prediction_scale)
        self.cropped_image = self.extract_minAreaRect(self.warped_image, self.image_rectangle,
                                                      scale=1 / self.prediction_scale)

        h, w = self.cropped_cardboard.shape[:2]
        if h < w:
            self.cropped_cardboard = self.cropped_cardboard.transpose(1, 0, 2)[::-1]
            self.cropped_image = self.cropped_image.transpose(1, 0, 2)[::-1]
            self.document_info.logger.info('Rotated the cardboard')

        # Performs the checks
        # h, w = self.cropped_cardboard.shape[:2]
        # if h < w:
        #    self.cropped_cardboard = self.cropped_cardboard.transpose(1, 0, 2)[::-1]
        #    self.document_info.logger.info('Rotated the cardboard')
        #    h, w = self.cropped_cardboard.shape[:2]
        # if not self._validate_height(h):
        #    self.document_info.logger.warning('Unusual cardboard height : {}'.format(h))
        # if not self._validate_width(w):
        #    self.document_info.logger.warning('Unusual cardboard width : {}'.format(w))
        # if not self._validate_ratio(h / w):
        #    self.document_info.logger.warning('Unusual cardboard ratio : {}'.format(h / w))

    def extract_minAreaRect(self, img, rect, scale):
        center, size, angle = rect
        # Find the closest angle to vertical
        while angle > 45:
            angle -= 90
            size = (size[1], size[0])
        while angle < -45:
            angle += 90
            size = (size[1], size[0])
        # Multiply sizes by the scale factor
        center = (center[0] * scale, center[1] * scale)
        size = (size[0] * scale, size[1] * scale)

        # Generates the transformation matrix
        T = np.array([[0, 0, center[0] - size[0] / 2], [0, 0, center[1] - size[1] / 2]])
        M = cv2.getRotationMatrix2D(center, angle, 1.0) - T
        # Perform the transformation
        return cv2.warpAffine(img, M, (round(size[0]), round(size[1])))

    def transform(self, xy):

        normalize = (np.max(xy[:, 0]) - np.min(xy[:, 0])) * (np.max(xy[:, 1]) - np.min(xy[:, 1]))
        x = xy[:, 1]
        y = xy[:, 0]
        radius = (np.square(x - self.center_x) + np.square(y - self.center_y)) / normalize
        coef_x = 1 + (radius * self.p[0][0]) + (np.square(radius) * self.p[0][1])
        coef_y = 1 + (radius * self.p[2][0]) + (np.square(radius) * self.p[2][1])

        add_x = ((self.p[1][1] * (radius + (2 * np.square(x)))) + (2 * self.p[1][0] * x * y)) * (
            1 + radius * self.p[1][2]) + (
                    np.square(radius) * self.p[1][3])
        add_y = ((self.p[3][1] * (radius + (2 * np.square(y)))) + (2 * self.p[3][0] * x * y)) * (
            1 + radius * self.p[3][2]) + (
                    np.square(radius) * self.p[3][3])

        x = (x * coef_x) + add_x
        y = (y * coef_y) + add_y
        xy = np.concatenate([y, x])

        return xy.astype(np.int32)

    def get_cardboard(self) -> Union['RectoCardboard', 'VersoCardboard']:
        assert self.cropped_cardboard is not None, 'Call crop_cardboard first'
        if self.document_info.side == 'recto':
            return RectoCardboard(self.document_info, self.cropped_cardboard)
        else:
            return VersoCardboard(self.document_info, self.cropped_cardboard)

    def get_image(self) -> 'ExtractedImage':
        assert self.cropped_image is not None, 'Call crop_image first'
        return ExtractedImage(self.document_info, self.cropped_image)

    def save_prediction(self, path=None):
        assert self.prediction is not None, 'Call crop_cardboard first'
        if path is None:
            self.document_info.check_output_folder()
            path = os.path.join(self.document_info.output_folder, self.output_prediction)
        plt.imsave(path, self.prediction)

    def save_extraction(self, path=None):
        assert self.prediction is not None, 'Call crop_cardboard first'
        if path is None:
            self.document_info.check_output_folder()
            path = os.path.join(self.document_info.output_folder, shared.EXTRACTION_THUMBNAIL_DEFAULT_FILENAME)
        output = self.resized_raw_scan.copy().astype(np.uint8)
        cv2.polylines(output, cv2.boxPoints(self.cardboard_rectangle).astype(np.int32)[None], True, (255, 0, 0), 4)
        cv2.polylines(output, cv2.boxPoints(self.image_rectangle).astype(np.int32)[None], True, (0, 0, 255), 4)
        utils.save_image(path, output)

    @staticmethod
    def _validate_width(width):
        return shared.CARDBOARD_MIN_WIDTH <= width <= shared.CARDBOARD_MAX_WIDTH

    @staticmethod
    def _validate_height(height):
        return shared.CARDBOARD_MIN_HEIGHT <= height <= shared.CARDBOARD_MAX_HEIGHT

    @staticmethod
    def _validate_ratio(ratio):
        return shared.CARDBOARD_MIN_RATIO <= ratio <= shared.CARDBOARD_MAX_RATIO
