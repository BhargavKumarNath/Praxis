"""Decision console (Phase 13, ADR 0021): read API per dashboard view plus the guarded admin
actions, served as its own process (``python -m praxis.console``).

It sits above ``api | science | agent``: it reads the stores of record and the fleet view
(``ops.system.SystemReader``), and completes guarded requests only through the existing
paths (``agent.requests.ApprovalDesk``, ``pricing.freeze.FreezeStore``). It can never set a
price, run the optimiser or execute a payment (import contract in ``pyproject.toml``).
"""
