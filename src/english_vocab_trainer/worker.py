"""Cloudflare Python Worker entrypoint."""

from english_vocab_trainer.web.app import app

worker = app
