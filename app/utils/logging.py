import logging
from logging.handlers import WatchedFileHandler
from flask import g, has_request_context, session


class RequestContextFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = getattr(g, "trace_id", "system") if has_request_context() else getattr(record, "trace_id", "system")
        record.user_id = session.get("user_id") if has_request_context() else getattr(record, "user_id", None)
        record.event_type = getattr(record, "event_type", "request")
        return True


def configure_logging(app):
    log_dir = app.config["LOG_DIR"]
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s trace_id=%(trace_id)s user_id=%(user_id)s event_type=%(event_type)s %(message)s"
    )
    context_filter = RequestContextFilter()
    logger_files = {
        app.logger: "app.log",
        logging.getLogger("labour_os.event"): "event.log",
        logging.getLogger("labour_os.error"): "error.log",
        logging.getLogger("labour_os.worker"): "worker.log",
    }
    for logger, filename in logger_files.items():
        log_path = (log_dir / filename).resolve()
        if not any(getattr(handler, "baseFilename", None) == str(log_path) for handler in logger.handlers):
            handler = WatchedFileHandler(log_path, encoding="utf-8")
            handler.setLevel(logging.INFO)
            handler.setFormatter(formatter)
            handler.addFilter(context_filter)
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    app.logger.setLevel(logging.INFO)
