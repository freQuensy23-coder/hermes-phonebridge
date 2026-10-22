import re
import sys


TAG = re.compile(r"refs/tags/v([1-9][0-9]*)")


def next_release(refs):
    numbers = [int(match.group(1)) for ref in refs if (match := TAG.fullmatch(ref.strip()))]
    return max(numbers, default=0) + 1


def main():
    print(f"number={next_release(sys.stdin.read().split())}")


if __name__ == "__main__":
    main()
