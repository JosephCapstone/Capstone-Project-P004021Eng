#!/usr/bin/env python3

import argparse
from pathlib import Path

from PIL import Image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--size", type=int, default=600)
    parser.add_argument("inputs", nargs="+")
    args = parser.parse_args()

    images = [
        Image.open(path).convert("RGB").resize((args.size, args.size))
        for path in args.inputs
    ]
    comparison = Image.new("RGB", (args.size * len(images), args.size), "white")
    for index, image in enumerate(images):
        comparison.paste(image, (index * args.size, 0))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    comparison.save(output)


if __name__ == "__main__":
    main()
