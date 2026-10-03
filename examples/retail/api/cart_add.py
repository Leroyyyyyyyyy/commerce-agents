"""Operation identities for the retail add button, separate from Agent turns."""

from __future__ import annotations

import asyncio
import copy
import weakref
from uuid import UUID

from fastapi import HTTPException
from pydantic import ConfigDict

from demo_common import CartAddRequest, StorefrontHost
from demo_common.storefront import StorefrontRecord

BUTTON_NOTE = (
    "Customer tapped the add-to-cart button on {title} ({product_id}), quantity {quantity}."
)


class RetailCartAddRequest(CartAddRequest):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID


class RetailCartAdds:
    def __init__(self, host: StorefrontHost) -> None:
        self.host = host
        # The default demo's replay cache has the same process lifetime as its cart.
        # Durable and cross-worker replay uses PostgresRetail.direct_add_once instead.
        self._completed: dict[tuple[str, str], tuple[dict, int, dict]] = {}
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def add(self, record: StorefrontRecord, request: RetailCartAddRequest) -> dict:
        body = request.model_dump(mode="json")
        durable_add = getattr(self.host.backend, "direct_add_once", None)
        if durable_add is not None:
            # Leave the dependency's record unchanged: SQL writes the latest session
            # note under its row lock, without overwriting a stale state/history copy.
            status, response = await durable_add(record.session_id, body, BUTTON_NOTE)
        else:
            status, response = await self._memory_add(record, request, body)
        if status != 200:
            raise HTTPException(status_code=status, detail=response["detail"])
        return response

    async def _memory_add(
        self, record: StorefrontRecord, request: RetailCartAddRequest, body: dict
    ) -> tuple[int, dict]:
        session_id = record.session_id
        key = (session_id, body["operation_id"])
        payload = request.model_dump(mode="json", exclude={"operation_id"})
        lock = self._locks.get(session_id)
        if lock is None:
            lock = self._locks[session_id] = asyncio.Lock()
        async with lock:
            completed = self._completed.get(key)
            if completed:
                original, status, response = completed
                if original != payload:
                    return 409, {
                        "detail": "Operation ID was already used with different parameters"
                    }
                return status, copy.deepcopy(response)
            current = self.host.sessions.require(session_id)
            try:
                response = await self.host.direct_add(current, request, note=BUTTON_NOTE)
                status = 200
            except HTTPException as error:
                status, response = error.status_code, {"detail": error.detail}
            # Save the replay identity before session write-back: a version conflict
            # after the in-memory cart write must not allow another add on retry.
            self._completed[key] = (payload, status, copy.deepcopy(response))
            self.host.sessions.save(current)
            return status, response
