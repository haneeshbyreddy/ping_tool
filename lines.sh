#!/usr/bin/env bash

# Loop through files and directories up to depth 2
find . -maxdepth 2 -not -path '*/.*' | sort | while read -r path; do
    # Calculate depth for indentation
    depth=$(tr -cd '/' <<< "${path#./}" | wc -c)
    
    # Skip the root dot itself
    [ "$path" = "." ] && continue
    
    # Indent based on depth
    indent=""
    [ "$depth" -eq 1 ] && indent="├── "
    [ "$depth" -eq 2 ] && indent="│   └── "

    # Calculate line count (ignore binary files, suppress errors for broken symlinks)
    if [ -d "$path" ]; then
        lines=$(find "$path" -type f -not -path '*/.*' -exec cat {} + 2>/dev/null | wc -l)
    else
        lines=$(wc -l < "$path" 2>/dev/null || echo 0)
    fi

    printf "%-50s [%s lines]\n" "${indent}$(basename "$path")" "$lines"
done
