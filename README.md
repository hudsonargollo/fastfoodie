# fastfoodie

Bilingual landing pages and pricing proposals for the ClubeMkt delivery-automation offer.

- `landing/` — the published pages. English and Spanish are twin files; keep their `<style>` blocks byte-identical.
- `server/` — `lead_service.py`, the stdlib-only endpoint behind `POST https://software.clubemkt.digital/api/lead`, plus its systemd unit.
- `Pricing-Proposal-*.md` / `.html` — emailed proposals, not published.

## Deploying

Pushing to GitHub does not update the live site — the VPS web root is not a checkout.

```
scp landing/<changed file> vps:/var/www/software-clubemkt-digital/
scp server/lead_service.py vps:/var/www/clubemkt-leads/ && ssh vps systemctl restart clubemkt-leads
```

The lead service stores submissions in `/var/lib/clubemkt-leads/leads.db` and is reachable
only through nginx on `127.0.0.1:8410`. There is no build step.
