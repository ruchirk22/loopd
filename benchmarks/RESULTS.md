# Benchmark results

Loop vs. a raw one-shot agent, same model, independent scoring. Regenerate and paste the
table here so it renders on GitHub:

```bash
python3 benchmarks/run_benchmark.py --model claude-opus-4-8 --repeat 3 --budget 8
cp "benchmarks/results/<timestamp>/summary.md" benchmarks/RESULTS.md   # then edit intro back in
git add benchmarks/RESULTS.md && git commit -m "benchmarks: publish results"
```

Methodology and how to add tasks: [benchmarks/README.md](README.md). Both arms run the same
model; success is decided by each task's independent `check.py`, never the agent's own tests.

> Read the numbers honestly. On easy tasks a raw agent also passes, so loopd shows equal
> success at higher cost — that's expected. loopd's value appears on tasks where the raw
> agent silently fails verification; report cost and time alongside success, and prefer a
> task set that actually discriminates.

## Latest run

_Not published yet — run the command above and paste the generated summary here._
