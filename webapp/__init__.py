"""Streamlit web application for the plant-readiness system.

Layout:

* ``app.py``            - entry point: navigation + page routing
* ``settings.py``       - secrets loading + repository factory
* ``auth.py``           - Supabase Auth session handling
* ``pages/*.py``        - one module per sidebar page
* ``components.py``     - shared display helpers (banners, tables)

Run locally with::

    streamlit run webapp/app.py

The UI never embeds SQL: every read/write goes through
:mod:`plantreadiness.repository` and :mod:`plantreadiness.services`, the
same service functions the command-line tools use.
"""
