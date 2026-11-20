# Fantasy Hockey Pool Dashboard

An interactive fantasy hockey dashboard for tracking standings, scoring trends,
and player performance.

[Open the interactive demo](https://donnie-mcfarlane.github.io/fantasy-hockey-pool-dashboard/)

The demo uses a scoring snapshot through November 19, 2026, with contestant
aliases. Interface improvements are published to the demo as they land.

The [active Batspool pool](https://batspool2027-private.pages.dev/) is protected
by Cloudflare Access and requires a one-time email passcode.

I'm happy to discuss setting up custom fantasy sports pools for others.
[Get in touch on GitHub](https://github.com/Donnie-McFarlane).

## Build the demo locally

Run `python3 render_chart.py --output _site` and open `_site/index.html`.

## Live deployment

Both builds use the same dashboard template and renderer. The live workflow
refreshes NHL scoring and deploys to Cloudflare Pages behind Access. It applies
real display names only during the live render.

For deployment, create a GitHub Actions environment named `live`, restricted
to the default branch. Add these environment secrets:

- `CONTESTANT_NAMES` maps `pool-01` through `pool-11` to real display names
  in the fixed order of contestants in `data/demo.json`.
- `CLOUDFLARE_API_TOKEN` has Cloudflare Pages Edit and Access Applications and
  Policies Read access for the account that owns the existing Pages project.
- `CLOUDFLARE_ACCOUNT_ID` identifies that Cloudflare account.
- `LIVE_STATE_KEY` is a randomly generated encryption passphrase for the
  scoring checkpoint in the `live-state` branch. Keep a private backup.

Run **Refresh private Batspool dashboard** manually to verify the first
deployment. Set the repository Actions variable `LIVE_ENABLED` to `true`
to enable the 3:00 a.m. Toronto run and the 5:15 a.m. retry.
