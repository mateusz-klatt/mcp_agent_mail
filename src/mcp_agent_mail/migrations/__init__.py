"""Alembic migration environment, shipped inside the package.

It lives under ``src/`` rather than at the repository root so that a wheel
carries it: a deployment that installs the package must be able to run
``alembic upgrade head`` without a checkout.
"""
