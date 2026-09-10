# Landing page

`index.html` is the project's landing page — one self-contained file, no build step, no
dependencies beyond a Google Fonts stylesheet.

## Publishing it

**Settings → Pages → Build and deployment → Source: `GitHub Actions`.**

That is the only setting to change. `.github/workflows/pages.yml` then deploys this
folder on every push to `main` that touches it.

Why not "deploy from a branch"? That option only offers the repository root or `/docs` —
there is no way to select `site/`. And `/docs` already holds the architecture documents,
which are written to be read on GitHub rather than served as a website.

## Editing it

The page shows real output from the tool — the cost ledger, the DNS doctor report, the
personalisation verification gate. If those formats change, update the page. A landing
page showing output the software no longer produces is worse than no landing page.

The workflow validates the document structure before deploying (doctype, balanced
`<body>`, stylesheet inside `<head>`, every CSS custom property declared in the bare
`:root` block). Written by hand with no build step, those are exactly the mistakes that
would otherwise ship silently — two of them did, once.
