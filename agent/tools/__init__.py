"""Tools the model can invoke via OpenAI-style structured tool calling.

The model never performs I/O itself (the tool-use boundary). Every function
here is plain, synchronous Python that the agent loop (``agent.loop``) calls
*after* parsing a tool-call request out of the model's message; the return
value is serialized back into the transcript as the tool's observation.

Six tools are exposed, across two toolkits:

- ``http_tool.HttpToolkit.http_request``          -- the required HTTP tool:
  the only way the agent touches the network.
- ``http_tool.HttpToolkit.list_visited_endpoints`` -- the chosen
  auxiliary tool: a coverage map of requests already made, grounded in what
  the agent actually did rather than in speculation.
- ``http_tool.HttpToolkit.list_id_candidates`` -- a single-hop,
  model-directed helper (eighteenth pass): given one already-fetched path,
  it extracts ID-shaped values from that cached body to suggest pivot
  targets, without issuing a new request.
- ``http_tool.HttpToolkit.discover_api_endpoints`` -- reads the SPA's bundled
  JavaScript (twenty-seventh pass) to surface crAPI's real API surface when
  blind noun-guessing stalls.
- ``control_tool.ControlToolkit.propose_finding``   -- how the model records a
  candidate finding; runs it through the scope gate (``agent.scope``) and
  validator (``agent.validation``) before accepting it.
- ``control_tool.ControlToolkit.finish_investigation`` -- the model's explicit
  completion signal (the "completion condition", alongside the hard
  step/token budget in ``agent.budget``).

See ``agent.toolbox`` for how these are assembled into the OpenAI tool-call
schema list and dispatched from the loop.
"""
