"""Backward-compatible entry point for the StageOps backup cron."""

from inbox.management.commands.backup_database import MAGIC, Command, encryption_key

__all__ = ["MAGIC", "Command", "encryption_key"]
