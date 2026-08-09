"""Cloudflare Python Worker ASGI entrypoint."""

from workers import WorkerEntrypoint, asgi

from english_vocab_trainer.web.app import app


class Default(WorkerEntrypoint):
    async def fetch(self, request: object) -> object:
        return await asgi.fetch(app, request, self.env)
