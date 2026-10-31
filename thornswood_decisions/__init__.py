"""The decision core: Patrick's cards, his answers and their receipts (07).

It runs on the Oracle VM as its own user, listens on loopback only, and is
reached from Patrick's phone through Tailscale Serve and from Hermes Desktop
through the thornswood-decisions plugin. It never runs on the game box or on
One. Every feature flag starts off.

Built in the order of 07 section 11.2, one pull request per step. Step 0 (this
package's first version) pins the Hermes contract in `contracts.json`.
"""
