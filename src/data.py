"""Data modules for segmentation task."""

import glob
import os
from pathlib import Path
from typing import Iterator

import albumentations as A
import cv2
import lightning as L
import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset

import settings
from utils import get_console_logger


def get_png_size(filepath: str) -> tuple:
    """Get the size of a PNG image parsing the header"""
    with open(filepath, "rb") as f:
        f.seek(16)
        width = int.from_bytes(f.read(4), "big")
        height = int.from_bytes(f.read(4), "big")
        return height, width


class SegmentationDataModule(L.LightningDataModule):
    """Custom lightning data module"""

    def __init__(self, data_config: dict) -> None:
        """Initializes the data module with the given configuration.

        Args:
            data_config (dict): Configuration for the data module.
        """
        super().__init__()
        self.save_hyperparameters(data_config)

    def setup(self, stage: str = None) -> None:
        """Sets up the data module for training, validation, and testing.

        Args:
            stage (str, optional): Stage of the data module. Can be "fit", "test", or None. Defaults to None.
        """
        image_width, image_height = (
            self.hparams["image_width"],
            self.hparams["image_height"],
        )
        apply_slicing = self.hparams["apply_slicing"]
        slice_width = self.hparams["slice_width"] if apply_slicing else None
        slice_height = self.hparams["slice_height"] if apply_slicing else None

        if stage == "fit" or stage is None:

            self.train_dataset = CustomTrainDataset(
                data_split_path=self.hparams["train"]["path"],
                transform_config=self.hparams["train"],
            )

            self.val_dataset = CustomValDataset(
                data_split_path=self.hparams["val"]["path"],
                transform_config=self.hparams["val"],
                image_width=image_width,
                image_height=image_height,
                apply_slicing=apply_slicing,
                slice_width=slice_width,
                slice_height=slice_height,
            )

            if self.hparams["dry_run"]:
                # NOTE: If your RAM is limited, you can use only 16 samples for dry run
                self.train_dataset.samples = self.train_dataset.samples[:16]
                self.val_dataset.samples = self.val_dataset.samples[:16]

        if stage == "test" or stage is None:
            # NOTE: As there are no labels for test split,
            #      we will use the same dataset as for validation
            self.test_dataset = CustomValDataset(
                data_split_path=self.hparams["val"]["path"],
                transform_config=self.hparams["val"],
                image_width=image_width,
                image_height=image_height,
                apply_slicing=apply_slicing,
                slice_width=slice_width,
                slice_height=slice_height,
            )

            if self.hparams["dry_run"]:
                # NOTE: If your RAM is limited, you can use only 16 samples for dry run
                self.test_dataset.samples = self.test_dataset.samples[:16]

    def train_dataloader(self) -> DataLoader:
        """Creates the training data loader.

        Returns:
            DataLoader: Data loader for the training dataset.
        """
        return DataLoader(
            self.train_dataset,
            batch_size=self.hparams["train"]["batch_size"],
            shuffle=self.hparams["train"]["shuffle"],
            num_workers=self.hparams["train"]["num_workers"],
            pin_memory=self.hparams["train"]["pin_memory"],
            persistent_workers=(
                True if self.hparams["train"]["num_workers"] > 0 else False
            ),
        )

    def val_dataloader(self) -> DataLoader:
        """Creates the validation data loader.

        Returns:
            DataLoader: Data loader for the validation dataset.
        """
        return DataLoader(
            self.val_dataset,
            batch_size=self.hparams["val"]["batch_size"],
            shuffle=self.hparams["val"]["shuffle"],
            num_workers=self.hparams["val"]["num_workers"],
            pin_memory=self.hparams["val"]["pin_memory"],
            persistent_workers=(
                True if self.hparams["val"]["num_workers"] > 0 else False
            ),
        )

    def test_dataloader(self) -> DataLoader:
        """Creates the testing data loader.

        Returns:
            DataLoader: Data loader for the validation* dataset.
        """
        return DataLoader(
            self.val_dataset,
            batch_size=self.hparams["val"]["batch_size"],
            shuffle=self.hparams["val"]["shuffle"],
            num_workers=self.hparams["val"]["num_workers"],
            pin_memory=self.hparams["val"]["pin_memory"],
            persistent_workers=(
                True if self.hparams["val"]["num_workers"] > 0 else False
            ),
        )


class CustomTrainDataset(Dataset):
    """Custom dataset for training"""

    def __init__(self, data_split_path: str, transform_config: dict):
        """Initializes the dataset with the given data split path and transform configuration.

        Args:
            data_split_path (str): Path to the data split folder.
            transform_config (dict): Configuration for data augmentation transforms.
        """
        super().__init__()

        self.parse_split_folder(data_split_path)
        self.parse_transform_config(transform_config)

        # NOTE: Dummy cache mechanism that speeds up training x100 times
        self.cache = {}

    def parse_split_folder(self, data_split_path: str) -> None:
        """Parses the data split folder and initializes the dataset samples.

        Args:
            data_split_path (str): Path to the data split folder.
        """

        self.samples = []

        # NOTE: Exclude 400 augmented samples "shifted*.png" and "flipped*.png"
        images = glob.glob(
            os.path.join(data_split_path, "**", "Images", "000*.png")
        ) + glob.glob(os.path.join(data_split_path, "**", "Images", "file*.png"))

        for image_path in images:
            label_path = image_path.replace("/Images/", "/Labels/")

            self.samples.append((image_path, label_path))

    def parse_transform_config(self, transform_config: dict) -> None:
        """Parses the transform configuration and initializes the transformation pipeline.

        Args:
            transform_config (dict): Configuration for data augmentation transforms.
        """

        self.transform = None

        if transform_config:
            self.transform = A.from_dict(transform_config)

    def __len__(self) -> int:
        """Gets the length of the dataset.

        Returns:
            int: Length of the dataset.
        """
        return len(self.samples)

    def load_image(self, image_path: str) -> np.ndarray:
        """Loads an image from the given path.

        Args:
            image_path (str): Path to the image.

        Returns:
            np.ndarray: Loaded image.
        """
        return cv2.imread(image_path)

    def load_label(self, label_path: str) -> np.ndarray:
        """Loads a label from the given path.
        The label is encoded to match the class encoding defined in settings.

        Args:
            label_path (str): Path to the label.

        Returns:
            np.ndarray: Encoded label.
        """
        label = cv2.imread(label_path)
        height, width = label.shape[:2]

        label = label.reshape(-1, 3)

        encoded_label = np.zeros((height, width), dtype=np.long)
        for class_idx, (class_name, pixel_value) in enumerate(
            settings.CLASS_ENCODING.items()
        ):
            encoded_class = np.all(label == pixel_value, axis=1, keepdims=True)
            encoded_class = encoded_class.reshape(height, width)

            encoded_label[encoded_class] = class_idx

        return encoded_label

    def __getitem__(self, idx: int) -> tuple:
        """Gets an item from the dataset.

        Args:
            idx (int): Index of the item to get.

        Returns:
            tuple: (image, label)
        """

        image_path, label_path = self.samples[idx]
        if image_path in self.cache:
            image, label = self.cache[image_path]
        else:
            image = self.load_image(image_path)
            label = self.load_label(label_path)
            self.cache[image_path] = (image, label)

        augmented = self.transform(image=image, mask=label)
        image = augmented["image"]
        label = augmented["mask"]

        return image, label.type(torch.long)


class CustomValDataset(Dataset):
    """Custom dataset for validation and testing"""

    def __init__(
        self,
        data_split_path: str,
        transform_config: dict,
        image_width: int,
        image_height: int,
        apply_slicing: bool = False,
        slice_width: int = None,
        slice_height: int = None,
    ) -> None:
        """Initializes the dataset with the given data split path, transform configuration,

        Args:
            data_split_path (str): Path to the data split folder.
            transform_config (dict): Configuration for data augmentation transforms.
            image_width (int): Width of the image.
            image_height (int): Height of the image.
            apply_slicing (bool, optional): Apply slicing method. Defaults to False.
            slice_width (int, optional): Width of slices. Defaults to None.
            slice_height (int, optional): Height of slices. Defaults to None.
        """
        super().__init__()

        self.image_width, self.image_height = image_width, image_height
        self.apply_slicing = apply_slicing
        self.slice_width, self.slice_height = slice_width, slice_height

        self.parse_split_folder(data_split_path)
        self.parse_transform_config(transform_config)

        # NOTE: Dummy cache mechanism that speeds up testing x100 times
        self.cache = {}

    def generate_slice_intervals(
        self, image_height: int, image_width: int, slice_height: int, slice_width: int
    ) -> list:
        """Generates the intervals for slicing the image.

        Args:
            image_height (int): Height of the image.
            image_width (int): Width of the image.
            slice_height (int): Height of the slice.
            slice_width (int): Width of the slice.

        Returns:
            list: List of tuples representing the intervals.
        """
        intervals = []

        # NOTE: It's ok if the slice end exceeds the image size, we will use paddning
        for i in range(0, image_height, slice_height):
            for j in range(0, image_width, slice_width):
                slice_height_start = i - max(0, ((i + slice_height) - image_height))
                slice_height_end = slice_height_start + slice_height

                slice_width_start = j - max(0, ((j + slice_width) - image_width))
                slice_width_end = slice_width_start + slice_width

                intervals.append(
                    (
                        slice(slice_height_start, slice_height_end),
                        slice(slice_width_start, slice_width_end),
                    )
                )

        return intervals

    def parse_split_folder(self, data_split_path: str) -> None:
        """Parses the data split folder and initializes the dataset samples.

        Args:
            data_split_path (str): Path to the data split folder.
        """

        self.samples = []

        for image_path in glob.glob(
            os.path.join(data_split_path, "**", "Images", "*.png")
        ):
            label_path = image_path.replace("/Images/", "/Labels/")

            if self.apply_slicing:
                intervals = self.generate_slice_intervals(
                    self.image_height,
                    self.image_width,
                    self.slice_height,
                    self.slice_width,
                )
            else:
                intervals = [(slice(None), slice(None))]

            for interval in intervals:
                self.samples.append((image_path, label_path, interval))

    def parse_transform_config(self, transform_config: dict) -> None:
        """Parses the transform configuration and initializes the transformation pipeline.

        Args:
            transform_config (dict): Configuration for data augmentation transforms.
        """

        self.transform = None

        if transform_config:
            self.transform = A.from_dict(transform_config)

    def __len__(self) -> int:
        """Gets the length of the dataset.

        Returns:
            int: Length of the dataset.
        """
        return len(self.samples)

    def load_image(self, image_path: str) -> np.ndarray:
        """Loads an image from the given path.

        Args:
            image_path (str): Path to the image.

        Returns:
            np.ndarray: Loaded image.
        """
        return cv2.imread(image_path)

    def load_label(self, label_path: str) -> np.ndarray:
        """Loads a label from the given path.
        The label is encoded to match the class encoding defined in settings.

        Args:
            label_path (str): Path to the label.

        Returns:
            np.ndarray: Encoded label.
        """
        label = cv2.imread(label_path)
        height, width = label.shape[:2]

        label = label.reshape(-1, 3)

        encoded_label = np.zeros((height, width), dtype=np.long)
        for class_idx, (class_name, pixel_value) in enumerate(
            settings.CLASS_ENCODING.items()
        ):
            encoded_class = np.all(label == pixel_value, axis=1, keepdims=True)
            encoded_class = encoded_class.reshape(height, width)

            encoded_label[encoded_class] = class_idx

        return encoded_label

    def __getitem__(self, idx: int) -> tuple:
        """Gets an item from the dataset.

        Args:
            idx (int): Index of the item to get.

        Returns:
            tuple: (image, label)
        """

        image_path, label_path, interval = self.samples[idx]

        if image_path in self.cache:
            image, label = self.cache[image_path]
        else:
            image = self.load_image(image_path)
            label = self.load_label(label_path)

            augmented = self.transform(image=image, mask=label)
            image = augmented["image"]
            label = augmented["mask"]

            self.cache[image_path] = (image, label)

        image = image[slice(None), *interval]
        label = label[interval]

        return image, label.type(torch.long)


class PredictionSource:
    """Custom class for handling prediction sources."""

    SUPPORTED_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"]
    SUPPORTED_VIDEO_EXTENSIONS = [".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv"]

    def __init__(self, source: str) -> None:
        self.source = Path(source)

        self.logger = get_console_logger("PredictionSource")

        self.load_source()

    def load_source(self) -> None:
        if not self.source.exists():
            raise FileNotFoundError(f"Source path does not exist: {self.source}")

        if self.source.is_file():
            suffix = self.source.suffix.lower()
            if suffix in self.SUPPORTED_IMAGE_EXTENSIONS:
                self._source_type = "image"
                self._items = [self.source]
                self._total_items = 1
            elif suffix in self.SUPPORTED_VIDEO_EXTENSIONS:
                self._source_type = "video"
                self._items = cv2.VideoCapture(str(self.source))

                if not self._items.isOpened():
                    raise ValueError(f"Cannot open video file: {self.source}")

                self._total_items = int(self._items.get(cv2.CAP_PROP_FRAME_COUNT))
            else:
                raise ValueError(f"Unsupported file type: {suffix}")
        elif self.source.is_dir():
            self._source_type = "folder"
            self._items = sorted(
                [
                    p
                    for p in self.source.iterdir()
                    if p.is_file()
                    and p.suffix.lower() in self.SUPPORTED_IMAGE_EXTENSIONS
                ]
            )

            if not self._items:
                raise FileNotFoundError(f"No images found in folder: {self.source}")
            self._total_items = len(self._items)
        else:
            raise ValueError(f"Unsupported source type: {self.source}")

        self.logger.info(
            f"Loaded {self._source_type} source with {self._total_items} items."
        )

    def __iter__(self) -> Iterator[np.ndarray]:
        """Returns the generator for iterating over processed images/batches."""
        return self._generator()

    def _generator(self) -> Iterator[np.array]:
        """Generator function to yield processed images/videos.

        Raises:
            ValueError: If the image cannot be read / Unsupported source type.

        Yields:
            Iterator[np.array]: Processed images/batches.
        """
        if self._source_type == "image" or self._source_type == "folder":
            paths = self._items
            for img_path in paths:
                img = cv2.imread(str(img_path))
                img_filename = img_path.name
                if img is None:
                    raise ValueError(f"Failed to read image: {img_path}")
                yield (img, img_filename)
        elif self._source_type == "video":
            frame_idx = 0
            while True:
                ret, frame = self._items.read()
                frame_name = self.source.name + f"_{frame_idx:04d}.jpg"
                if not ret:
                    break
                yield (frame, frame_name)
        else:
            raise ValueError(f"Unsupported source type: {self._source_type}")

    def __len__(self) -> int:
        """Get the total number of items in the source.

        Returns:
            int: Total number of items in the source.
        """
        return self._total_items

    def __del__(self) -> None:
        """Ensure video capture is released if the object is destroyed."""
        if self._source_type == "video" and isinstance(self._items, cv2.VideoCapture):
            if self._items.isOpened():
                self._items.release()


if __name__ == "__main__":
    import yaml
    from tqdm import tqdm

    from utils import load_config

    config = load_config("configs/config.yaml")

    data_split_config = config["data"]

    data_module = SegmentationDataModule(data_split_config)
    data_module.setup(stage="fit")

    train_loader = data_module.train_dataloader()
    val_loader = data_module.val_dataloader()

    for batch in tqdm(train_loader):
        images, labels = batch
        print(images.shape, labels.shape)
        print(images.dtype, labels.dtype)

        break

    for batch in tqdm(val_loader):
        images, labels = batch
        print(images.shape, labels.shape)
        print(images.dtype, labels.dtype)
        break
