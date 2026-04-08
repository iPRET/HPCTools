# HPCTools

My vibecoded tools for working with HPCs, not intended for public usage.

## inspectjobs.py

Auto-refreshing SLURM job monitor. Detects your username and associated accounts, then displays all jobs grouped by project every 20 seconds.

```bash
# Autodetect accounts
python inspectjobs.py

# Or specify accounts manually
python inspectjobs.py jureap133
```
