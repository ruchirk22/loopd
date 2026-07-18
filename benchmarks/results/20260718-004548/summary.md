# Benchmark results (haiku)

## Summary by arm

| arm | runs | success rate | mean cost | mean wall |
|---|---|---|---|---|
| baseline | 3 | 3/3 (100%) | $0.0638 | 35s |
| loop | 3 | 2/3 (67%) | $1.1985 | 446s |

## By task

| task | arm | success | mean cost | mean wall |
|---|---|---|---|---|
| fizzbuzz | baseline | 1/1 | $0.0552 | 31s |
| fizzbuzz | loop | 0/1 | $1.5378 | 542s |
| primes | baseline | 1/1 | $0.0735 | 40s |
| primes | loop | 1/1 | $1.0569 | 427s |
| roman | baseline | 1/1 | $0.0628 | 35s |
| roman | loop | 1/1 | $1.0007 | 370s |

## Every run

| task | arm | rep | success | cost | wall | detail | note |
|---|---|---|---|---|---|---|---|
| fizzbuzz | baseline | 1 | ✓ | $0.0552 | 30.8s | turns=9 |  |
| fizzbuzz | loop | 1 | ✗ | $1.5378 | 542.1s | steps=2/2 skipped=0 attempts=4 replans=1 |  |
| primes | baseline | 1 | ✓ | $0.0735 | 40.0s | turns=9 |  |
| primes | loop | 1 | ✓ | $1.0569 | 427.0s | steps=2/2 skipped=0 attempts=2 replans=1 |  |
| roman | baseline | 1 | ✓ | $0.0628 | 34.6s | turns=8 |  |
| roman | loop | 1 | ✓ | $1.0007 | 370.4s | steps=3/3 skipped=0 attempts=3 replans=0 |  |
