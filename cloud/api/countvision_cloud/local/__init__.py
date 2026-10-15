"""CountVision Local: the offline edition on one Windows PC (switched on with CV_EDITION=local).

The same API and web app as the cloud, with these differences:

* store.py       Redis replaced by an in-process store (rate limits, live updates, device polls).
* background.py  Celery worker + beat replaced by threads in the API process.
* licence.py     Ed25519-signed licence keys instead of Stripe (14-day trial, 2 cameras without one).
* setup.py       first-start wizard (owner account, business, site, first camera) and the built-in
                 edge agent, paired automatically (its token is written into the edge's folder).
* backup.py      backup and restore of the database (pg_dump / pg_restore) to a folder or USB stick.
* router.py      /api/local/* for the web app.

Cloud-only features are hidden: billing, Google login, Telegram, the operator admin page and the
public sign-up page. Nothing here runs in the cloud edition.
"""
