"""The decision core: Patrick's cards, his answers and their receipts.

It runs on the Bots' VM as its own user, listens on loopback only, and is
reached from Patrick's phone through Tailscale Serve and from Hermes Desktop
through the thornswood-decisions plugin. It never runs on the game box. Every
feature flag starts off. docs/decision-app.md describes each part.
"""
