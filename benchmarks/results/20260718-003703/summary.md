# Benchmark results (claude-opus-4-8)

## Summary by arm

| arm | runs | success rate | mean cost | mean wall |
|---|---|---|---|---|
| baseline | 9 | 0/9 (0%) | $0.0000 | 2s |
| loop | 9 | 0/9 (0%) | $0.0000 | 9s |

## By task

| task | arm | success | mean cost | mean wall |
|---|---|---|---|---|
| fizzbuzz | baseline | 0/3 | $0.0000 | 2s |
| fizzbuzz | loop | 0/3 | $0.0000 | 9s |
| primes | baseline | 0/3 | $0.0000 | 2s |
| primes | loop | 0/3 | $0.0000 | 9s |
| roman | baseline | 0/3 | $0.0000 | 2s |
| roman | loop | 0/3 | $0.0000 | 9s |

## Every run

| task | arm | rep | success | cost | wall | detail | note |
|---|---|---|---|---|---|---|---|
| fizzbuzz | baseline | 1 | ✗ | $0.0 | 2.7s | turns=1 | exit 1 |
| fizzbuzz | baseline | 2 | ✗ | $0.0 | 1.9s | turns=1 | exit 1 |
| fizzbuzz | baseline | 3 | ✗ | $0.0 | 1.9s | turns=1 | exit 1 |
| fizzbuzz | loop | 1 | ✗ | $0.0 | 9.0s | steps=0/0 skipped=0 attempts=0 replans=0 |  |
| fizzbuzz | loop | 2 | ✗ | $0.0 | 8.3s | steps=0/0 skipped=0 attempts=0 replans=0 |  |
| fizzbuzz | loop | 3 | ✗ | $0.0 | 8.6s | steps=0/0 skipped=0 attempts=0 replans=0 |  |
| primes | baseline | 1 | ✗ | $0.0 | 1.9s | turns=1 | exit 1 |
| primes | baseline | 2 | ✗ | $0.0 | 2.5s | turns=1 | exit 1 |
| primes | baseline | 3 | ✗ | $0.0 | 1.9s | turns=1 | exit 1 |
| primes | loop | 1 | ✗ | $0.0 | 8.4s | steps=0/0 skipped=0 attempts=0 replans=0 |  |
| primes | loop | 2 | ✗ | $0.0 | 8.8s | steps=0/0 skipped=0 attempts=0 replans=0 |  |
| primes | loop | 3 | ✗ | $0.0 | 8.5s | steps=0/0 skipped=0 attempts=0 replans=0 |  |
| roman | baseline | 1 | ✗ | $0.0 | 2.1s | turns=1 | exit 1 |
| roman | baseline | 2 | ✗ | $0.0 | 2.0s | turns=1 | exit 1 |
| roman | baseline | 3 | ✗ | $0.0 | 1.9s | turns=1 | exit 1 |
| roman | loop | 1 | ✗ | $0.0 | 8.2s | steps=0/0 skipped=0 attempts=0 replans=0 |  |
| roman | loop | 2 | ✗ | $0.0 | 10.7s | steps=0/0 skipped=0 attempts=0 replans=0 |  |
| roman | loop | 3 | ✗ | $0.0 | 8.4s | steps=0/0 skipped=0 attempts=0 replans=0 |  |
