import logging
from logging.handlers import WatchedFileHandler


def configure_logging(app):
    log_dir = app.config["LOG_DIR"]
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (log_dir / "application.log").resolve()
    if not any(getattr(handler, "baseFilename", None) == str(log_path)
               for handler in app.logger.handlers):
        handler = WatchedFileHandler(log_path, encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ))
        app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)
