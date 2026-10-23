import re
import sys


TAG = re.compile(r"refs/tags/v([1-9][0-9]*)")


def next_release(lines, revision):
    numbers = []
    current = []
    for line in lines:
        fields = line.split()
        match = TAG.fullmatch(fields[0]) if fields else None
        if match is None:
            continue
        number = int(match.group(1))
        numbers.append(number)
        if fields[1:] == [revision]:
            current.append(number)
    return max(current) if current else max(numbers, default=0) + 1


def main():
    print(f"number={next_release(sys.stdin.read().splitlines(), sys.argv[1])}")


if __name__ == "__main__":
    main()
