"""Evaluation: hand labels (count-helper), accuracy on labeled clips, tuning, speed benchmark.

    countvision-edge count-helper VIDEO      label the true counts by hand
    countvision-edge eval fetch               download the clip videos
    countvision-edge eval tune --detector ..  tune on the "tune" clips only
    countvision-edge eval run  --detector ..  score the "test" clips
    countvision-edge bench --model ..         speed on this machine
    countvision-edge eval report              write eval/results.md

See eval/README.md in the repository.
"""
