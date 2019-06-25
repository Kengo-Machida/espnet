import logging
import json
from glob import glob
topk = 10
score = "validation/main/acc"

def f(nbpe):
    print(f"[INFO] nbpe {nbpe}")
    pattern = f"exp/train_960*nbpe{nbpe}*/results/log"

    results = []
    for path in glob(pattern):
        try:
            with open(path, "r") as f:
                log = json.load(f)
        except IOError:
            continue

        acc = 0
        epoch = 0
        for l in log:
            if score in l:
                epoch += 1
                acc = max(acc, l[score])
        results.append(dict(path=path, acc=acc, epoch=epoch))

    print(f"[INFO] total {len(results)} log found")
    for l in sorted(results, key=lambda x: -x["acc"])[:topk]:
        print(l)

f(5000)
f(16000)
