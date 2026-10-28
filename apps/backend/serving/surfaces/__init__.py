"""Protocol execution shared by the online routes and the batch worker (issue #1690).

Each inference protocol -- Messages, Chat Completions, Embeddings, Responses --
has one execution function that both its HTTP route and the batch dispatcher
call. What they share is only the execution context in :mod:`.context`; the
requests, options, results and streams stay protocol-specific.
"""
