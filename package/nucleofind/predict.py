import logging
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' 
from typing import List, Tuple
import tensorflow as tf
import numpy as np
import gemmi
from tqdm import tqdm
import time
import argparse
import site 
from glob import glob
import sys

class Prediction:
    def __init__(self, model_dir: str, use_cache: bool = True):
        self.use_cache: bool = use_cache
        self.model_dir: str = model_dir
        self.model_name: str = model_dir.split("/")[-1]

        self.predicted_map: np.ndarray = None

        self.predicted_grid: gemmi.FloatGrid = None

        self.pdb_code = ""

        self.na: float = 0
        self.nb: float = 0
        self.nc: float = 0
        self.translation_list: List[List[int]] = []

        self.model: tf.keras.Model = None

        self.interpolated_grid: gemmi.FloatGrid = None
        self.raw_grid: gemmi.FloatGrid = None
        self.map: gemmi.Ccp4Map = None
        self.structure: gemmi.Structure = None
        self.transform: gemmi.Transform = None
        self.box_minimum: gemmi.PositionBox = None

    def make_prediction(self, file_path: str, column_labels: List[str] = ["FWT", "PHWT"],
                        resolution_cutoff: float = None, use_raw_values: bool = False):
        start = time.time()
        try:
            self._load_model()
        except OSError:
            print("This model is corrupted, perhaps due to an incomplete download. Try downloading it again with nucleofind-install -m TYPE --reinstall")
            sys.exit()

        if column_labels == [None, None]:
            column_labels = ["FWT", "PHWT"]

        if ".mtz" in file_path:
            if resolution_cutoff:
                resolution_cutoff = float(resolution_cutoff)
            self._load_mtz(file_path, resolution_cutoff, column_labels)
        elif ".map" in file_path:
            self._load_map(file_path)
        else:
            raise RuntimeError("The input file is not a mtz or map.")

        self._interpolate_grid()
        self._calculate_translations(overlap=16)
        self._predict(raw_values=use_raw_values, overlap=16)

        self.predicted_grid = self._reinterpolate_to_output(self.predicted_map)

        end = time.time()
        delta = end - start
        logging.info(f"Prediction took - {delta:.3f}")

    def save_predicted_map(self, output_path: str):
        ccp4 = gemmi.Ccp4Map()
        ccp4.grid = self.predicted_grid
        ccp4.update_ccp4_header()

        ccp4.write_ccp4_map(output_path)

    def save_interpolated_map(self, output_path: str, grid_spacing: float = 0.7):
        logging.info("Saving interpolated map")
        ccp4 = gemmi.Ccp4Map()
        ccp4.grid = self.interpolated_grid
        ccp4.update_ccp4_header()

        ccp4.write_ccp4_map(output_path)


    def save_raw_map(self, output_path: str):
        ccp4 = gemmi.Ccp4Map()
        ccp4.grid = self.raw_grid
        ccp4.update_ccp4_header()

        ccp4.write_ccp4_map(output_path)

    def _load_model(self):
        
        def sigmoid_focal_crossentropy(y_true, y_pred, alpha = 0.25, gamma = 2.0, from_logits: bool = False):
            if gamma and gamma < 0:
                raise ValueError("Value of gamma should be greater than or equal to zero.")

            y_pred = tf.convert_to_tensor(y_pred)
            y_true = tf.cast(y_true, dtype=y_pred.dtype)

            # Get the cross_entropy for each entry
            ce = tf.keras.backend.binary_crossentropy(y_true, y_pred, from_logits=from_logits)

            # If logits are provided then convert the predictions into probabilities
            if from_logits:
                pred_prob = tf.sigmoid(y_pred)
            else:
                pred_prob = y_pred

            p_t = (y_true * pred_prob) + ((1 - y_true) * (1 - pred_prob))
            alpha_factor = 1.0
            modulating_factor = 1.0

            if alpha:
                alpha = tf.cast(alpha, dtype=y_true.dtype)
                alpha_factor = y_true * alpha + (1 - y_true) * (1 - alpha)

            if gamma:
                gamma = tf.cast(gamma, dtype=y_true.dtype)
                modulating_factor = tf.pow((1.0 - p_t), gamma)

            # compute the final loss and return
            return tf.reduce_sum(alpha_factor * modulating_factor * ce, axis=-1)

        logging.info(f"Loading model from file/folder: {self.model_dir}")
        if os.path.isdir(self.model_dir):
            logging.info("Loading model from model folder")
            print(self.model_dir, type(self.model_dir))
            self.model = tf.keras.models.load_model(
                self.model_dir,
                custom_objects={
                    "sigmoid_focal_crossentropy": sigmoid_focal_crossentropy
                },
            )
        elif os.path.isfile(self.model_dir):
            logging.info("Loading model from weight file")
            self.model = tf.keras.models.load_model(
                self.model_dir,
                custom_objects={
                    "sigmoid_focal_crossentropy": sigmoid_focal_crossentropy
                },
                compile=False,
            )
        else:
            raise RuntimeError(f"The model file was not found! {self.model_dir}")

    def _load_map(self, map_path: str, normalise: bool = True):
        self.map: gemmi.Ccp4Map = gemmi.read_ccp4_map(map_path)
        self.raw_grid: gemmi.FloatGrid = self.map.grid

        if normalise:
            self.raw_grid.normalize()

    def _load_mtz(self, mtz_path: str, resolution_cutoff: float, column_names: List[str] = ["FWT", "PHWT"]):
        mtz = gemmi.read_mtz_file(mtz_path)
        logging.info("Reading mtz file ", mtz_path)
        self.raw_grid = mtz.transform_f_phi_to_map(*column_names)
        # "sfcalc.F_phi.F", "sfcalc.F_phi.phi"
        if resolution_cutoff:
            data = np.array(mtz, copy=False)
            mtz.set_data(data[mtz.make_d_array() >= resolution_cutoff])

        # self.raw_grid = mtz.transform_f_phi_to_map("FWT", "PHWT")

    def _get_bounding_box(self, grid: gemmi.FloatGrid) -> gemmi.PositionBox:
        extent = gemmi.find_asu_brick(grid.spacegroup).get_extent()

        # unit_cell = grid.unit_cell

        extent.maximum = gemmi.Fractional(1, 1, 1)
        extent.minimum = gemmi.Fractional(0, 0, 0)

        corners = [
            grid.unit_cell.orthogonalize(fractional)
            for fractional in (
                extent.minimum,
                gemmi.Fractional(extent.maximum[0], extent.minimum[1], extent.minimum[2]),
                gemmi.Fractional(extent.minimum[0], extent.maximum[1], extent.minimum[2]),
                gemmi.Fractional(extent.minimum[0], extent.minimum[1], extent.maximum[2]),
                gemmi.Fractional(extent.maximum[0], extent.maximum[1], extent.minimum[2]),
                gemmi.Fractional(extent.maximum[0], extent.minimum[1], extent.maximum[2]),
                gemmi.Fractional(extent.minimum[0], extent.maximum[1], extent.maximum[2]),
                extent.maximum,
            )
        ]
        min_x = min(corner[0] for corner in corners)
        min_y = min(corner[1] for corner in corners)
        min_z = min(corner[2] for corner in corners)
        max_x = max(corner[0] for corner in corners)
        max_y = max(corner[1] for corner in corners)
        max_z = max(corner[2] for corner in corners)

        box = gemmi.PositionBox()
        box.minimum = gemmi.Position(min_x, min_y, min_z)
        box.maximum = gemmi.Position(max_x, max_y, max_z)
        return box

    def _interpolate_grid(self, grid_spacing: float = 0.7):
        box: gemmi.PositionBox = self._get_bounding_box(self.raw_grid)
        size: gemmi.Position = box.get_size()

        logging.info(f"Raw unit cell is : {self.raw_grid.unit_cell}")

        logging.debug(f"Box size : {size}")

        num_x = int(size.x / grid_spacing)
        num_y = int(size.y / grid_spacing)
        num_z = int(size.z / grid_spacing)

        logging.debug(f"Num x,y,z: {num_x}, {num_y}, {num_z}")

        array = np.zeros((num_x, num_y, num_z), dtype=np.float32)
        scale = gemmi.Mat33(
            [[grid_spacing, 0, 0], [0, grid_spacing, 0], [0, 0, grid_spacing]]
        )

        self.transform: gemmi.Transform = gemmi.Transform(scale, box.minimum)
        self.raw_grid.interpolate_values(array, self.transform)
        cell: gemmi.UnitCell = gemmi.UnitCell(size.x, size.y, size.z, 90, 90, 90)
        self.interpolated_grid = gemmi.FloatGrid(array, cell)

        self.box_minimum = box.minimum

        logging.debug(f"Interpolated grid (numpy) shape: {array.shape}")
        logging.debug(f"Interpolated grid (gemmi) shape: {self.interpolated_grid}")

    def _reinterpolate_to_output(self, grid_to_interp: np.ndarray) -> gemmi.FloatGrid:
        logging.info("Reinterpolating array")
        # logging.debug(np.unique(grid_to_interp, return_index=True))
        # Taken from https://github.com/paulsbond/densitydensenet/blob/main/predict.py - Paul Bond

        dummy_structure = gemmi.Structure()
        dummy_structure.cell = self.raw_grid.unit_cell
        dummy_structure.spacegroup_hm = self.raw_grid.spacegroup.hm
        output_grid = gemmi.FloatGrid()
        output_grid.setup_from(dummy_structure, spacing=0.7)

        size_x = grid_to_interp.shape[0] * 0.7
        size_y = grid_to_interp.shape[1] * 0.7
        size_z = grid_to_interp.shape[2] * 0.7

        grid_to_interp[np.isnan(grid_to_interp)] = 0

        array_cell = gemmi.UnitCell(size_x, size_y, size_z, 90, 90, 90)
        array_grid = gemmi.FloatGrid(grid_to_interp, array_cell)

        for point in output_grid.masked_asu():
            position = output_grid.point_to_position(point) - self.box_minimum
            point.value = array_grid.interpolate_value(position)

        output_grid.symmetrize_max()

        return output_grid

    def _calculate_translations(self, overlap: int = 32):
        logging.info("Calculating translations")
        logging.debug(
            f"Interpolated grid unit cell : {self.interpolated_grid.unit_cell}"
        )
        logging.debug(
            f"Interpolated grid shape : {self.interpolated_grid.array.shape}"
        )
        overlap_na: float = (self.interpolated_grid.array.shape[0] // overlap) + 1
        overlap_nb: float = (self.interpolated_grid.array.shape[1] // overlap) + 1
        overlap_nc: float = (self.interpolated_grid.array.shape[2] // overlap) + 1

        logging.debug(f"overlap na, nb, nc: {overlap_na}, {overlap_nb}, {overlap_nc}")

        for x in range(int(overlap_na)):
            for y in range(int(overlap_nb)):
                for z in range(int(overlap_nc)):
                    self.translation_list.append(
                        [x * overlap, y * overlap, z * overlap]
                    )

        self.na: float = (self.interpolated_grid.array.shape[0] // 32) + 1
        self.nb: float = (self.interpolated_grid.array.shape[1] // 32) + 1
        self.nc: float = (self.interpolated_grid.array.shape[2] // 32) + 1
        logging.debug(f"na, nb, nc: {self.na}, {self.nb}, {self.nc}")

        logging.debug(f"Translation list size: {len(self.translation_list)}")

    def _predict(self, raw_values: bool = True, overlap: int = 32):
        logging.info("Predicting map")

        predicted_map = np.zeros(
            (
                int(32 * self.na) + (32 - overlap), int(32 * self.nb) + (32 - overlap),
                int(32 * self.nc) + (32 - overlap)),
            np.float32,
        )
        count_map = np.zeros(
            (
                int(32 * self.na) + (32 - overlap), int(32 * self.nb) + (32 - overlap),
                int(32 * self.nc) + (32 - overlap)),
            np.float32,
        )

        logging.debug(f"Predicted map shape - {predicted_map.shape}")

        # quit()

        for translation in tqdm(self.translation_list, total=len(self.translation_list)):
            x, y, z = translation
            # logging.debug(
            #     f"Predicting {x}, {y}, {z} -> {x + 32}, {y + 32}, {z + 32}, where final shape is {predicted_map.shape}"
            # )

            sub_array = np.array(
                self.interpolated_grid.get_subarray(
                    start=translation, shape=[32, 32, 32]
                )
            ).reshape((1, 32, 32, 32, 1))

            if np.sum(sub_array) == 0:
                predicted_map[x: x + 32, y: y + 32, z: z + 32] += np.zeros(32, 32, 32)
                count_map[x: x + 32, y: y + 32, z: z + 32] += 1
                continue

            predicted_sub = self.model.predict(sub_array, verbose=0).squeeze()
            arg_max = np.argmax(predicted_sub, axis=-1)

            # Taken from https://github.com/paulsbond/densitydensenet/blob/main/predict.py
            if raw_values:
                predicted_map[x: x + 32, y: y + 32, z: z + 32] += predicted_sub[
                                                                  :, :, :, 1
                                                                  ]
            else:
                predicted_map[x: x + 32, y: y + 32, z: z + 32] += arg_max

            count_map[x: x + 32, y: y + 32, z: z + 32] += 1
            
        predicted_map = predicted_map[
            0:(32 * self.na),
            0:(32 * self.nb),
            0:(32 * self.nc),
        ]

        count_map = count_map[
            0:(32 * self.na),
            0:(32 * self.nb),
            0:(32 * self.nc),
        ]
        logging.debug(f"Predicted map shape: {predicted_map.shape}")
        self.predicted_map = predicted_map / count_map


def run():
    logging.basicConfig(
        level=logging.CRITICAL, format="%(asctime)s %(levelname)s - %(message)s"
    )

    start = time.time()

    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "-model", help="Model selection", required=False)
    parser.add_argument("-i", "-input", help="Input mtz", required=True)
    parser.add_argument("-o", "-output", help="Output map", required=True)
    parser.add_argument("-r", "-resolution", nargs='?', help="Resolution cutoff")
    parser.add_argument("-intensity", nargs='?', help="Name of intensity column in MTZ")
    parser.add_argument("-phase", nargs='?', help="Name of phase column in MTZ")
    parser.add_argument("-model_path", nargs='?', help="Path to model (development)")

    args = vars(parser.parse_args())

    if not args["model_path"]:
        model_path = find_model(args["m"])
    else:
        model_path = args["model_path"]

    if not model_path:
        if not args["m"] or not os.path.exists(args["m"]):
            raise FileNotFoundError("Model path could not be found, check the supplied path")
        model_path = args["m"]
    else:
        logging.info(f"Found model at path: {model_path}, continuing using this model...")

    if not os.path.isfile(args["i"]):
        raise FileNotFoundError(
            f"Input file has not been found, check path\nPath Supplied {args['i']} from {os.getcwd()}")

    prediction = Prediction(model_dir=model_path, use_cache=False)

    prediction.make_prediction(args["i"], [args["intensity"], args["phase"]])

    prediction.save_predicted_map(args["o"])

    end = time.time()

    print(f"Time taken {end - start:.2f} seconds / {(end - start) / 60:.2f} minutes")


def model_not_found_err():
    print("""
            No models have been found in either site_packages or CCP4/lib/data.
            You can install models using the command:
            nucleofind-install -o site_packages -m phos
        """)

def find_all_potential_models():

    model_extension = "*.hdf5"

    potential_models = []


    for pkg in site.getsitepackages():
        models = glob(os.path.join(pkg, "nucleofind_models" , model_extension))
        potential_models += models

    clibd = os.environ.get('CLIBD', "")
    if not os.path.exists(clibd) and not potential_models:
        print(
            """CCP4 Environment Variable - CLIBD is not found. 
            You can try sourcing it: 
            Ubuntu - source /opt/xtal/ccp4-X.X/bin/ccp4.setup-sh
            MacOS - source /Applications/ccp4-X.X/bin/ccp4.setup-sh
            """
            )
        return
    
    
    ccp4_model_path = os.path.join(clibd, "nucleofind_models")
    if not os.path.exists(ccp4_model_path) and not potential_models:
        model_not_found_err()
        return
    
    potential_models += glob(os.path.join(ccp4_model_path, model_extension))

    if not potential_models:
        model_not_found_err()

    return potential_models


def find_model(model_selection: str) -> str:

    models = find_all_potential_models()
    if not models: 
        print("""No models were found, please use 
    nucleofind-install -o site_packages --all
to install all the models or
    nucleofind-install -m {phos,sugar,base} -o site_packages
to install a single model (choose either phos, sugar or base)
              """)
        exit()

    if model_selection:
        for model in models: 
            filename = model.split("/")[-1]
            if model_selection in filename:
                return model
        
    if len(models) == 1:
        print("Only found ", models[0], "- using that!")
        return models[0]

    print(f"The specified model type '{model_selection}' could not be found, please add one of the following flags")

    filenames = set([x.split("/")[-1].rstrip(".hdf5") for x in models])
    for name in filenames:
        print(f"-m {name}")

    exit()

if __name__ == "__main__":

    run()
