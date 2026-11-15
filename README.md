# Economics of AI Jobs Board

A public page of early-career jobs, internships and fellowships in the economics and policy of AI, in the UK or fully remote. It is made for students on Economics of AI (7SSPN409), MSc Economics and Policy, King's College London.

**Live page:** https://boukektkcl.github.io/economics-of-ai-jobs/

## How it works

- `sources.json` lists the employers whose public careers feeds are read.
- `scripts/collect.py` downloads those feeds every morning, keeps the vacancies that pass the filters, and writes `site/data/jobs.json`.
- `site/index.html` is the page students see. It reads `site/data/jobs.json`.
- `.github/workflows/update.yml` runs the script every day at 05:23 UTC and republishes the page.

## Filters

1. **Place**: based in the UK, or remote and open to people living in the UK.
2. **Seniority**: no senior titles, no advert asking for more than 3 years of experience, no PhD requirement.
3. **Role**: economics, policy, research and analysis roles.
4. **AI focus**: at employers that are not AI organisations, the advert itself has to be about AI, technology or digital markets.

The word lists behind each filter are near the top of the "filters" section in `scripts/collect.py`.

## Common changes

- **Add or remove an employer**: edit `sources.json` (instructions are at the top of the file). The page rebuilds within a few minutes of saving.
- **Refresh now**: Actions tab, "Update jobs board", "Run workflow".
- **See why an advert was kept or dropped**: open the latest run on the Actions tab and download the `audit` file.
- **Change the logo**: put the file at `site/assets/kcl-logo.svg` (or `.png`).
