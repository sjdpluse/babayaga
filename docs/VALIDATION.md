# Verification record — 2026-09-12

- 41 automated tests pass locally under Python 3.12 / NumPy 2.3.5.
- PPO gradients were compared against finite differences; actual training updates
  policy weights. Checkpoint reload and tampering rejection are tested.
- An end-to-end synthetic fixture runs collection, three expanding training/
  validation splits, a final fit and an untouched final holdout. Synthetic models
  are rejected for demo eligibility. This is a software test, not market evidence.
- The first GitHub Actions run passed on commit
  `6a64956ddbce2e17a9c470f676635b2f3ab44d69` (40 tests before the additional
  automatic-startup-preflight test):
  https://github.com/sjdpluse/babayaga/actions/runs/34674616358
- Railway built the actual Dockerfile, installed NumPy and reported SUCCESS for
  deployment `e6ef5214-2342-4b4e-9e12-3839fc123921`.
- Dedicated Railway project: `babayaga`; service: `babayaga-worker`; source: main
  branch of this repository. No API/database secrets were configured by this task.
- No authenticated TheTrueTrade call was made: the local connectivity command
  exited with the explicit missing-environment-credentials diagnostic.
- No exchange order, demo trade, real historical training or profitability claim.
- Supabase schema is prepared but unapplied and unverified on a live database.
- The optional Gymnasium adapter has not been run in this local environment.
- Railway state has no attached durable volume yet; collection and training are
  disabled. Attach durable writable storage before enabling them.

For the next run, add credentials through Railway Variables. Startup performs the
profile/futures preflight and retains the exchange-write block. See ACTIVATION.md
for the substantive demo-contract, adapter and persistence work still required.
