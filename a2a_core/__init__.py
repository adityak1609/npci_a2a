"""NPCI A2A protocol layer: envelope schemas, transport auth, task state, audit.

Named ``a2a_core`` (not ``a2a``) because the Google A2A SDK already owns the
top-level ``a2a`` package name — a local ``a2a/`` directory would shadow it.

Import submodules directly (e.g. ``from a2a_core.models import A2AEnvelope``).
"""
