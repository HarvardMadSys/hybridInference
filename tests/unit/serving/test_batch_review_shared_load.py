"""The shared-load reader must not mix two publication snapshots."""

from contextlib import ExitStack

import pytest

from serving.batch.interactive_load import InteractiveLoad, SharedInteractiveLoad


@pytest.mark.asyncio
async def test_publish_between_shared_and_own_reads_cannot_hide_other_gateway_load():
    local = InteractiveLoad()
    remote_load = 2
    with ExitStack() as requests:

        class PublishingBetweenQueries:
            async def interactive_load(self, *, ttl_seconds, exclude_process=None):
                # The single snapshot of every *other* process's rows.
                assert exclude_process == "review-local"
                snapshot = {"model": remote_load}
                # While the read yields to DB I/O, three local requests arrive
                # and the publisher commits their counts before the next query.
                for _ in range(3):
                    requests.enter_context(local.track("model"))
                return snapshot

            async def interactive_load_of(self, *, process_id, ttl_seconds):
                return {"model": 3}

        shared = SharedInteractiveLoad(
            PublishingBetweenQueries(), process_id="review-local", local=local
        )
        observed = await shared.read()
        assert observed == {"model": remote_load + local.in_flight("model")}
