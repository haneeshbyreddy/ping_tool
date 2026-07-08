#!/usr/bin/env bash

find . -maxdepth 2 -not -path '*/.*' | sort | while read -r path; do
    depth=$(tr -cd '/' <<< "${path#./}" | wc -c)

    [ "$path" = "." ] && continue

    indent=""
    [ "$depth" -eq 1 ] && indent="├── "
    [ "$depth" -eq 2 ] && indent="│   └── "

    if [ -d "$path" ]; then
        lines=$(find "$path" -type f -not -path '*/.*' -exec cat {} + 2>/dev/null | wc -l)
    else
        lines=$(wc -l < "$path" 2>/dev/null || echo 0)
    fi

    printf "%-50s [%s lines]\n" "${indent}$(basename "$path")" "$lines"
done
