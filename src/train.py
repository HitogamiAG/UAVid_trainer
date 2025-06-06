import datetime as dt
from warnings import filterwarnings

import comet_ml  # type: ignore # To get all data logged automatically
import lightning as L
from lightning.pytorch.loggers import CometLogger

import settings
from data import SegmentationDataModule
from model import SegmentationModel
from utils import (
    create_dir_safely,
    export_model_to_onnx,
    finish_comet_run,
    get_callback,
    get_console_logger,
    get_loggers,
    load_config,
    parse_train_args,
)

if __name__ == "__main__":

    filterwarnings("ignore")
    console_logger = get_console_logger("TrainLogger")

    # --- Parse command line arguments ---
    args = parse_train_args()

    # --- Load Hyperparameters ---
    config = load_config(args.config)
    data_config = config["data"]
    training_config = config["trainer"]

    console_logger.info(f"Loaded configuration from {settings.CONFIG_PATH}")

    # --- Set up run name ---
    run_name = (
        f"{training_config['model']['arch']}_{training_config['model']['encoder_name']}"
    )
    run_name += f"_{dt.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
    console_logger.info(f"Generated run name: {run_name}")

    # --- Track experiment into local folder ---
    dirpath = f"{settings.TRAIN_LOG_DIR}/{run_name}"
    create_dir_safely(dirpath)

    # --- Reproducibility ---
    L.seed_everything(
        seed=training_config["common"]["seed"], workers=True, verbose=False
    )
    console_logger.info(f"Set random seed to {training_config['common']['seed']}")

    # --- Initialize Data Module ---
    data_module = SegmentationDataModule(data_config=data_config)
    console_logger.info("Initialized DataModule")

    # --- Initialize Trainer module ---
    model = SegmentationModel(trainer_config=training_config)
    console_logger.info("Initialized Model")

    # --- Initialize Experiment Logger ---
    loggers = get_loggers(logger_configs=training_config["loggers"], run_name=run_name)
    console_logger.info("Initialized Experiment Loggers")

    # --- Initialize Callbacks ---
    # This callback is mandatory
    model_checkpoint = get_callback(
        "ModelCheckpoint",
        {"dirpath": dirpath, **training_config["callbacks"]["model_checkpoint"]},
    )

    callbacks = [
        # These callbacks are optional
        get_callback(
            callback_config["callback_name"], callback_config["callback_params"]
        )
        for callback_config in training_config["callbacks"]["optional_callbacks"]
    ]
    callbacks.append(model_checkpoint)
    console_logger.info("Initialized Callbacks")

    # --- Initialize Trainer ---
    trainer = L.Trainer(
        accelerator=training_config["common"]["accelerator"],
        devices=training_config["common"]["devices"],
        max_epochs=training_config["common"]["epochs"],
        logger=loggers,
        callbacks=callbacks,
        precision=training_config["common"]["precision"],
        log_every_n_steps=training_config["common"]["log_every_n_steps"],
        limit_train_batches=4 if data_config["dry_run"] else None,
        limit_val_batches=4 if data_config["dry_run"] else None,
        limit_test_batches=4 if data_config["dry_run"] else None,
    )
    console_logger.info("Initialized Trainer")

    # --- Train the model ---
    console_logger.info("Starting training...")
    trainer.fit(model, datamodule=data_module)
    console_logger.info("Training finished.")

    # --- Best model checkpoint ---
    best_model_path = model_checkpoint.best_model_path

    # --- Test the model ---
    console_logger.info("Starting testing...")
    trainer.test(datamodule=data_module, ckpt_path=best_model_path)
    console_logger.info("Testing finished.")

    # --- Export the best model ---
    if training_config["common"]["export_onnx"]:
        console_logger.info("Exporting model to ONNX format...")
        onnx_export_path = f"{dirpath}/model.onnx"
        half = training_config["common"]["export_onnx_fp16"]

        model = SegmentationModel.load_from_checkpoint(
            best_model_path,
            trainer_config=training_config,
        )

        export_model_to_onnx(
            model,
            input_tensor=next(iter(data_module.test_dataloader()))[0],
            export_path=onnx_export_path,
            half=half,
        )
        best_model_path = onnx_export_path
        console_logger.info(f"Model exported to {onnx_export_path}")

    # --- End Comet Experiment (if used) ---
    if isinstance(loggers, list):
        for logger in loggers:
            if isinstance(logger, CometLogger):
                finish_comet_run(logger, best_model_path)
                console_logger.debug("Comet experiment ended.")

    console_logger.info("Script finished.")
