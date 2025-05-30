import os
from urllib.parse import urlparse

import lightning as L
import numpy as np
import onnxruntime as ort
import requests
import segmentation_models_pytorch as smp
import torch
import torch.nn as nn
import torch.optim as optim
import torchmetrics

from utils import get_console_logger


class SegmentationModel(L.LightningModule):
    """Lightning Module for Segmentation Models using Segmentation Models PyTorch (SMP)"""

    def __init__(self, trainer_config):
        super().__init__()
        self.save_hyperparameters(trainer_config)

        self.init_model()
        self.init_loss()
        self.init_metrics()

    def init_model(self) -> None:
        """Initialize the model based on the provided hyperparameters."""
        self.model = smp.create_model(**self.hparams.model)

    def init_loss(self) -> None:
        """Initialize the loss function based on the provided hyperparameters."""
        loss_name = self.hparams["loss"]["loss_name"]
        if loss_name == "dice":
            loss_class = smp.losses.DiceLoss
        elif loss_name == "jaccard":
            loss_class = smp.losses.JaccardLoss
        elif loss_name == "ce":
            loss_class = nn.CrossEntropyLoss
        elif loss_name == "focal":
            loss_class = smp.losses.FocalLoss
        else:
            raise ValueError(f"Unsupported loss function: {loss_name}")

        self.loss_fn = loss_class(**self.hparams["loss"]["loss_params"])

    def init_metrics(self) -> None:
        """Initialize metrics based on the provided hyperparameters."""
        metric_params = self.hparams["metrics"]["metric_params"]

        self.train_iou = torchmetrics.JaccardIndex(**metric_params)
        self.val_iou = torchmetrics.JaccardIndex(**metric_params)
        self.test_iou = torchmetrics.JaccardIndex(**metric_params)

        self.train_acc = torchmetrics.Accuracy(**metric_params, average="macro")
        self.val_acc = torchmetrics.Accuracy(**metric_params, average="macro")
        self.test_acc = torchmetrics.Accuracy(**metric_params, average="macro")

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Same as torch.nn.Module.forward"""
        return self.model(x)

    def _common_step(self, batch: tuple, batch_idx: int, stage: str) -> torch.Tensor:
        """Common step for training, validation, and testing."""
        images, masks = batch
        logits = self(images)

        loss = self.loss_fn(logits, masks)

        preds = torch.argmax(logits, dim=1)  # (N, H, W)

        if stage == "train":
            iou = self.train_iou(preds, masks)
            acc = self.train_acc(preds, masks)
            self.log(
                f"{stage}_loss",
                loss,
                on_step=True,
                on_epoch=True,
                prog_bar=False,
                logger=True,
            )
            self.log(
                f"{stage}_iou",
                iou,
                on_step=False,
                on_epoch=True,
                prog_bar=True,
                logger=True,
            )
            self.log(
                f"{stage}_acc",
                acc,
                on_step=False,
                on_epoch=True,
                prog_bar=True,
                logger=True,
            )
        elif stage == "val":
            iou = self.val_iou(preds, masks)
            acc = self.val_acc(preds, masks)
            self.log(
                f"{stage}_loss",
                loss,
                on_step=False,
                on_epoch=True,
                prog_bar=True,
                logger=True,
            )
            self.log(
                f"{stage}_iou",
                iou,
                on_step=False,
                on_epoch=True,
                prog_bar=True,
                logger=True,
            )
            self.log(
                f"{stage}_acc",
                acc,
                on_step=False,
                on_epoch=True,
                prog_bar=True,
                logger=True,
            )
        elif stage == "test":
            iou = self.test_iou(preds, masks)
            acc = self.test_acc(preds, masks)
            self.log(f"{stage}_loss", loss, on_step=False, on_epoch=True, logger=True)
            self.log(f"{stage}_iou", iou, on_step=False, on_epoch=True, logger=True)
            self.log(f"{stage}_acc", acc, on_step=False, on_epoch=True, logger=True)

        return loss

    def training_step(self, batch: tuple, batch_idx: int) -> torch.Tensor:
        """Training step for the model."""
        return self._common_step(batch, batch_idx, "train")

    def validation_step(self, batch: tuple, batch_idx: int) -> torch.Tensor:
        """Validation step for the model."""
        return self._common_step(batch, batch_idx, "val")

    def test_step(self, batch: tuple, batch_idx: int) -> torch.Tensor:
        """Test step for the model."""
        return self._common_step(batch, batch_idx, "test")

    def configure_optimizers(self) -> optim.Optimizer | tuple:
        """Choose what optimizers and learning-rate schedulers to use in your optimization."""
        optimizer_name = self.hparams["optimizer"]["optimizer_name"]
        if optimizer_name == "adam":
            optimizer_class = optim.Adam
        elif optimizer_name == "adamw":
            optimizer_class = optim.AdamW
        elif optimizer_name == "sgd":
            optimizer_class = optim.SGD
        else:
            raise ValueError(f"Unsupported optimizer: {self.hparams.optimizer}")

        optimizer = optimizer_class(
            self.parameters(), **self.hparams["optimizer"]["optimizer_params"]
        )

        if self.hparams["scheduler"]["use_scheduler"]:
            scheduler_name = self.hparams["scheduler"]["scheduler_name"]
            if scheduler_name == "reducelronplateau":
                scheduler = optim.lr_scheduler.ReduceLROnPlateau(
                    optimizer, **self.hparams["scheduler"]["scheduler_params"]
                )
                return {
                    "optimizer": optimizer,
                    "lr_scheduler": scheduler,
                    "monitor": self.hparams["scheduler"]["monitor"],
                }
            elif scheduler_name == "cosineannealinglr":
                scheduler = optim.lr_scheduler.CosineAnnealingLR(
                    optimizer,
                    T_max=self.hparams["common"]["epochs"],
                    **self.hparams["scheduler"]["scheduler_params"],
                )
                return [optimizer], [scheduler]
            else:
                raise ValueError(f"Unsupported scheduler: {scheduler_name}")
        else:
            return optimizer


class PredictionModel:
    """Prediction model for image segmentation tasks."""

    DEFAULT_MEAN = np.array([0.485, 0.456, 0.406])  # ImageNet mean
    DEFAULT_STD = np.array([0.229, 0.224, 0.225])  # ImageNet std

    def __init__(
        self,
        model_source: str,
        num_classes: int,
        batch_size: int,
        image_crop_size: int,
        intersection_ratio: float,
        half: bool = False,
    ):
        """Initialize the PredictionModel.

        Args:
            model_source (str): Path to the model file or URL.
            num_classes (int): Number of classes for segmentation.
            batch_size (int): Batch size for inference.
            image_crop_size (int): Size of the image crop.
            intersection_ratio (float): Ratio of intersection for cropping.
            half (bool, optional): Only for Pytorch Inference! Use FP16. Defaults to False.
        """

        self.model_source = model_source
        self.num_classes = num_classes
        self.batch_size = batch_size
        self.image_crop_size = image_crop_size
        self.intersection_ratio = intersection_ratio
        self.half = half

        self.logger = get_console_logger(__name__)

        self.load_model()

    def download_model(self, url: str) -> str:
        """Download the model from a URL and save it to a temporary file.

        Args:
            url (str): URL of the model file.

        Returns:
            str: Path to the downloaded model file.
        """
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path)

        # Create full path in /tmp directory
        filepath = os.path.join("/tmp", filename)

        # Download and save the file
        response = requests.get(url, stream=True)
        response.raise_for_status()  # Raise exception for HTTP errors

        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:  # Filter out keep-alive chunks
                    f.write(chunk)

        return filepath

    def load_model(self) -> None:
        """Load the model from the specified source."""
        if self.model_source.startswith("http"):
            model_path = self.download_model(self.model_source)
            self.model_source = model_path

        if self.model_source.endswith(".ckpt"):
            self._engine = "torch"

            checkpoint = torch.load(self.model_source)
            # Remove 'model.' prefix from state_dict keys
            checkpoint["state_dict"] = {
                k[6:]: v for k, v in checkpoint["state_dict"].items()
            }

            self.model = smp.create_model(**checkpoint["hyper_parameters"]["model"])
            self.model.load_state_dict(checkpoint["state_dict"])
            self.model.eval()

            if self.half:
                self.model.half()

        elif self.model_source.endswith(".onnx"):
            self._engine = "ort"

            available_providers = ort.get_available_providers()
            if "CUDAExecutionProvider" in available_providers:
                self._providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            else:
                self.logger.warning(
                    "CUDAExecutionProvider not available. Using CPUExecutionProvider."
                )

                if "LD_LIBRARY_PATH" not in os.environ:
                    self.logger.warning(
                        "LD_LIBRARY_PATH not set. This may cause issues with CUDAExecutionProvider."
                    )

                self._providers = ["CPUExecutionProvider"]

            self.model = ort.InferenceSession(
                self.model_source, providers=self._providers
            )
            self._input_name = self.model.get_inputs()[0].name
            self._output_name = self.model.get_outputs()[0].name

            self._dtype = (
                self.model.get_inputs()[0].type.replace("tensor(", "").replace(")", "")
            )
            try:
                self._dtype = np.dtype(self._dtype)
            except TypeError:
                self.logger.warning(
                    f"ORT expected input type not understood: {self._dtype}. Defaulting to float32."
                )
                self._dtype = np.float32

            if self.half:
                pass
        else:
            raise ValueError(f"Unsupported model format: {self.model_source}")

    def generate_slice_intervals(
        self,
        image_height: int,
        image_width: int,
        image_crop_size: int,
        intersect_ratio: float,
    ) -> list:
        """Generates the intervals for slicing the image.

        Args:
            image_height (int): Height of the image.
            image_width (int): Width of the image.
            image_crop_size (int): Size of the crop.

        Returns:
            list: List of tuples representing the intervals.
        """
        intervals = []

        intersection_size = int(image_crop_size * intersect_ratio)
        step_size = image_crop_size - intersection_size

        # NOTE: It's ok if the slice end exceeds the image size, we will use paddning
        #       During concatenation, we will ignore the padded part
        for i in range(0, image_height, step_size):
            for j in range(0, image_width, step_size):
                intervals.append(
                    (slice(i, i + image_crop_size), slice(j, j + image_crop_size))
                )

        return intervals

    def apply_slicing(self, image: np.ndarray) -> tuple:
        """Apply slicing to the image based on the specified intervals.

        Args:
            image (np.ndarray): Input image.

        Returns:
            tuple: Tuple containing the sliced images and the intervals.
        """

        image_height, image_width = image.shape[:2]

        intervals = self.generate_slice_intervals(
            image_height, image_width, self.image_crop_size, self.intersection_ratio
        )

        images = []

        for interval in intervals:
            crop = image[interval]
            if (
                crop.shape[0] < self.image_crop_size
                or crop.shape[1] < self.image_crop_size
            ):
                pad_height = max(0, self.image_crop_size - crop.shape[0])
                pad_width = max(0, self.image_crop_size - crop.shape[1])
                crop = np.pad(
                    crop, ((0, pad_height), (0, pad_width), (0, 0)), mode="constant"
                )
            images.append(crop)

        return np.array(images), intervals

    def concatenate_slices(
        self, slices_probs: np.ndarray, intervals: list, image_shape: tuple
    ) -> np.ndarray:
        """Concatenate the slices back to the original image shape.

        Args:
            slices_probs (np.ndarray): Segmentation masks of probs of classes.
            intervals (list): List of intervals used for slicing.
            image_shape (tuple): Original image shape.

        Returns:
            np.ndarray: Concatenated segmentation mask.
        """

        slices_probs = np.moveaxis(slices_probs, (1, 2), (-1, -2))
        mask_probs = np.zeros(
            (image_shape[0], image_shape[1], self.num_classes), dtype=np.uint8
        )

        for slice_probs, slice_interval in zip(slices_probs, intervals):
            pad_height = max(0, slice_interval[0].stop - image_shape[0])
            pad_width = max(0, slice_interval[1].stop - image_shape[1])
            slice_probs = slice_probs[
                : slice_interval[0].stop - slice_interval[0].start - pad_height,
                : slice_interval[1].stop - slice_interval[1].start - pad_width,
            ]

            mask_probs[slice_interval] += slice_probs

        mask = np.argmax(mask_probs, axis=-1)
        return mask

    def predict_torch(self, images: np.ndarray) -> np.ndarray:
        """Perform inference using PyTorch.

        Args:
            images (np.ndarray): Input images for prediction.

        Returns:
            np.ndarray: Predicted segmentation masks.
        """

        images_tensor = torch.from_numpy(images).float()

        if self.half:
            images_tensor = images_tensor.half()

        masks_probs = np.zeros(
            (
                images_tensor.shape[0],
                self.num_classes,
                images_tensor.shape[2],
                images_tensor.shape[3],
            ),
            dtype=np.float32,
        )
        for i in range(0, images_tensor.shape[0], self.batch_size):
            with torch.no_grad():
                masks_probs[i : i + self.batch_size] = (
                    self.model(images_tensor[i : i + self.batch_size]).cpu().numpy()
                )

        return masks_probs

    def predict_ort(self, images: np.ndarray) -> np.ndarray:
        """Perform inference using ONNX Runtime.

        Args:
            images (np.ndarray): Input images for prediction.

        Returns:
            np.ndarray: Predicted segmentation masks.
        """

        images = images.astype(self._dtype)

        masks_probs = np.zeros(
            (images.shape[0], self.num_classes, images.shape[2], images.shape[3]),
            dtype=self._dtype,
        )
        for i in range(0, images.shape[0], self.batch_size):
            masks_probs[i : i + self.batch_size] = self.model.run(
                [self._output_name], {self._input_name: images[i : i + self.batch_size]}
            )[0]

        return masks_probs

    def predict(self, image: np.ndarray) -> np.ndarray:
        """Predict the segmentation mask for the given image.

        Args:
            image (np.ndarray): Input image for prediction.

        Returns:
            np.ndarray: Predicted segmentation mask.
        """

        image_tensor, intervals = self.apply_slicing(image)

        image_tensor = np.moveaxis(image_tensor, (1, 2), (-1, -2))

        # Normalize the images
        image_tensor = image_tensor.astype(np.float32)
        image_tensor = image_tensor / 255.0
        image_tensor = (
            image_tensor - self.DEFAULT_MEAN.reshape(1, 3, 1, 1)
        ) / self.DEFAULT_STD.reshape(1, 3, 1, 1)

        if self._engine == "ort":
            self.logger.info("Using ONNX Runtime for inference.")
            masks_probs = self.predict_ort(image_tensor)
        elif self._engine == "torch":
            self.logger.info("Using PyTorch for inference.")
            masks_probs = self.predict_torch(image_tensor)
        else:
            raise ValueError(f"Unsupported engine: {self._engine}")

        return self.concatenate_slices(masks_probs, intervals, image.shape[:2])


if __name__ == "__main__":
    model1 = PredictionModel(
        "runs/train/unet_mit_b0_2025-05-09_23-42-26/model.onnx", 8, 8, 224, 0.2, True
    )
    model2 = PredictionModel(
        "runs/train/unet_mit_b0_2025-05-09_23-42-26/best_model.ckpt", 8, 8, 224, 0.2
    )

    import cv2

    for i in range(4):
        image = np.random.sample((1024, 1536, 3)).astype(np.float32)
        mask1 = model1.predict(image)
        mask2 = model2.predict(image)

        cv2.imwrite(f"mask1_{i}.png", (mask1 / 8 * 255).astype(np.uint8))
        cv2.imwrite(f"mask2_{i}.png", (mask2 / 8 * 255).astype(np.uint8))
        print(mask1.shape, mask2.shape)
