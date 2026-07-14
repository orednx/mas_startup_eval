# MAS Startup Eval

[Русский](README.md) | [English](README.en.md)

A multi-agent system on the Claude API for evaluating the prospects of Russian tech startups (0–3 scale: closed / surviving without growth / growing / leader-exit).

## Data

`Data/Startups.xlsx` — 352 startups (168 from a top-tier support program + 184 control group), a single table, single sheet. Fields: project description, keywords, market/regulatory context, funding-need description, team metadata, and patent/publication activity for the underlying technology topic (Russia + foreign, 2018–2024).

The real outcome and group membership (top-program vs. control) are not physically present in this file — they're kept in a separate file outside the repo and used only for post-hoc validation, never as a second sheet in the same table.

## Structure

```
prompts/     system prompts for the 5 agents (Scout → Technologist/Market/Team → Integrator)
src/         pipeline (src/pipeline.py)
Data/        Startups.xlsx — input table
results/     run output (per-startup JSON + results.csv, not in repo)
```

## Running

```
pip install -r requirements.txt
cp .env.example .env   # set ANTHROPIC_API_KEY

python src/pipeline.py --input Data/Startups.xlsx --limit 20
```

Flags: `--input` (path to the table), `--output-dir` (default `results`), `--limit` (cap the number of startups for debugging).

## Architecture

Agents never access the internet and never see the real outcome or group label — only the dataset fields explicitly whitelisted per agent in `pipeline.py`. Each agent returns JSON against a fixed schema (`output_config.format`), so parsing is reliable without retries on malformed output.

| Agent | Reads from the table | + |
|---|---|---|
| Scout | Name, суть_проекта, ключевые_слова_nlp, рынок, основатель, вуз, регион, год | — |
| Technologist | pat_rus_\*, pub_\*, pat_foreign_\* | Scout's profile |
| Market | рынок, инвестиции, регион, год | Scout's profile |
| Team | основатель, вуз, регион, год | Scout's profile |
| Integrator | none directly | Scout's profile + all three specialist outputs |

## Validation

`Уровень_исхода_0_3` (the real outcome, 0–3) and the group label (top-program / control) live in a separate private file, not under `Data/`. After a run they're checked against the Integrator's score: F1, recall, quadratic weighted kappa (ordinal scale) — plus a comparison of scores between the top-program and control groups (the study's core hypothesis).
