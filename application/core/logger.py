"""
Centralized logging system for Billy Bass Assistant.
Supports different log levels: ERROR, WARNING, INFO, VERBOSE
"""

import os
from enum import Enum


class LogLevel(Enum):
    ERROR = 0
    WARNING = 1
    INFO = 2
    VERBOSE = 3


class BillyLogger:
    def __init__(self):
        self._level = LogLevel.INFO  # Default level
        self._cached_level = None  # Cache the level to avoid repeated env lookups

    def _get_current_level(self):
        """Get the current log level, loading from environment if needed."""
        # Always check the environment variable to pick up changes
        level_str = os.getenv("LOG_LEVEL", "INFO").upper()
        try:
            return LogLevel[level_str]
        except KeyError:
            print(f"⚠️ Invalid LOG_LEVEL '{level_str}', using INFO")
            return LogLevel.INFO

    def set_level(self, level: LogLevel):
        """Set the current log level."""
        self._level = level
        os.environ["LOG_LEVEL"] = level.name

    def reload_level(self):
        """Force reload the log level from environment variable."""
        self._cached_level = None  # Clear cache
        return self._get_current_level()

    def get_level(self) -> LogLevel:
        """Get the current log level."""
        return self._get_current_level()

    def _should_log(self, level: LogLevel) -> bool:
        """Check if a message at this level should be logged."""
        current_level = self._get_current_level()
        return level.value <= current_level.value

    def _log(self, level: LogLevel, message: str, emoji: str = ""):
        """Internal logging method."""
        if self._should_log(level):
            prefix = f"{emoji} " if emoji else ""
            print(f"{prefix}{message}", flush=True)

    def error(self, message: str, emoji: str = "❌"):
        """Log an error message."""
        self._log(LogLevel.ERROR, message, emoji)

    def warning(self, message: str, emoji: str = "⚠️"):
        """Log a warning message."""
        self._log(LogLevel.WARNING, message, emoji)

    def info(self, message: str, emoji: str = "ℹ️"):
        """Log an info message."""
        self._log(LogLevel.INFO, message, emoji)

    def verbose(self, message: str, emoji: str = "🔍"):
        """Log a verbose/debug message."""
        self._log(LogLevel.VERBOSE, message, emoji)

    def success(self, message: str, emoji: str = "✅"):
        """Log a success message (INFO level)."""
        self._log(LogLevel.INFO, message, emoji)

    def debug(self, message: str, emoji: str = "🐛"):
        """Log a debug message (VERBOSE level)."""
        self._log(LogLevel.VERBOSE, message, emoji)


# Global logger instance
logger = BillyLogger()


def set_log_level(level: LogLevel):
    """Set the global log level."""
    logger.set_level(level)


def get_log_level() -> LogLevel:
    """Get the current global log level."""
    return logger.get_level()


def reload_log_level() -> LogLevel:
    """Force reload the log level from environment variable."""
    return logger.reload_level()


# Convenience functions for backward compatibility
def log_error(message: str, emoji: str = "❌"):
    logger.error(message, emoji)


def log_warning(message: str, emoji: str = "⚠️"):
    logger.warning(message, emoji)


def log_info(message: str, emoji: str = "ℹ️"):
    logger.info(message, emoji)


def log_verbose(message: str, emoji: str = "🔍"):
    logger.verbose(message, emoji)


def log_success(message: str, emoji: str = "✅"):
    logger.success(message, emoji)


def log_debug(message: str, emoji: str = "🐛"):
    logger.debug(message, emoji)
