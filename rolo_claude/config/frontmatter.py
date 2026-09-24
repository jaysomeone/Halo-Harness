from __future__ import annotations
import re
from typing import Any


def parse(text: str) -> tuple[dict, str]:
    """
    Parse YAML-like frontmatter from text.
    
    Returns:
        tuple[dict, str]: (frontmatter_dict, body_text) where frontmatter_dict contains
        parsed key-value pairs and body_text is everything after the closing '---'.
        If no frontmatter is found, returns ({}, text).
    """
    # Strip BOM, normalize CRLF/CR -> LF (finding 11: a CRLF file's first
    # line is "---\r\n", which doesn't start with the literal "---\n" this
    # parser looks for), then leading whitespace.
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').lstrip()

    # Check for opening delimiter
    if not text.startswith('---\n'):
        return {}, text
    
    # Find closing delimiter
    lines = text.splitlines()
    if len(lines) < 2:
        return {}, text
    
    # Skip opening delimiter line
    end_line = -1
    for i in range(1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        # Closing delimiter is exactly '---' (ignoring trailing whitespace)
        if stripped == '---':
            end_line = i
            break
    
    if end_line == -1:
        return {}, text  # No closing delimiter
    
    # Parse frontmatter lines (between opening and closing delimiters)
    frontmatter_lines = lines[1:end_line]
    frontmatter_text = '\n'.join(frontmatter_lines)
    
    # Parse YAML subset
    result = _parse_yaml_subset(frontmatter_text)
    
    # Get body (everything after closing delimiter)
    body_lines = lines[end_line + 1:]
    # Strip at most one leading blank line
    if body_lines and body_lines[0].strip() == '':
        body_lines = body_lines[1:]
    body = '\n'.join(body_lines)
    
    return result, body


def _parse_yaml_subset(text: str) -> dict[str, Any]:
    """Parse the YAML subset described in the spec."""
    result = {}
    lines = text.splitlines()
    i = 0
    
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        
        # Skip empty lines and comments
        if not stripped or stripped.startswith('#'):
            i += 1
            continue
        
        # Find key and colon
        colon_pos = line.find(':')
        if colon_pos == -1:
            # No colon on this line - treat as continuation or skip
            i += 1
            continue
        
        key = line[:colon_pos].rstrip()
        value_part = line[colon_pos + 1:].lstrip()
        
        # Handle block scalars (| or >)
        if value_part.startswith('|') or value_part.startswith('>'):
            block_type = value_part[0]
            # Get the indent level of the key
            key_indent = len(line) - len(line.lstrip())
            
            # Collect block content
            block_lines = []
            j = i + 1
            while j < len(lines):
                next_line = lines[j]
                # Check if line is at same or less indentation than key
                if next_line.strip() == '':
                    # Empty line in block - keep it
                    block_lines.append('')
                    j += 1
                elif len(next_line) - len(next_line.lstrip()) > key_indent:
                    # More indented than key - part of block
                    block_lines.append(next_line[key_indent + 2:])  # +2 for standard 2-space indent
                    j += 1
                else:
                    break
            
            i = j - 1  # Move i to last line of block
            
            if block_type == '|':
                # Literal block - preserve newlines
                value = '\n'.join(block_lines)
            else:  # '>'
                # Folded block - join with single spaces
                folded = []
                current = []
                for bline in block_lines:
                    if bline == '':
                        if current:
                            folded.append(' '.join(current))
                            current = []
                        folded.append('')
                    else:
                        current.append(bline)
                if current:
                    folded.append(' '.join(current))
                value = '\n'.join(folded)
            
            result[key] = value
        
        # Handle inline list -- single physical line only (`key: [a, b, c]`),
        # per spec; a value_part that starts with '[' but doesn't close on
        # the SAME line falls through to the plain-scalar branch below
        # rather than being (incorrectly) treated as a list.
        # NOTE: the original draft tried to hunt for the closing ']' by
        # indexing into `text` (the whole frontmatter block) using
        # `colon_pos` -- an index into just THIS line -- which silently
        # produced garbage/never matched, so no key was ever written for
        # this branch at all. Fixed to work purely off `value_part`.
        elif value_part.startswith('[') and value_part.rstrip().endswith(']'):
            list_content = value_part.rstrip()[1:-1].strip()
            if not list_content:
                result[key] = []
            else:
                # Split by commas, respecting quoted commas.
                items = []
                current_item = ''
                in_quotes = False
                quote_char = None

                for char in list_content:
                    if char in '\'"' and not in_quotes:
                        in_quotes = True
                        quote_char = char
                        current_item += char
                    elif char == quote_char and in_quotes:
                        in_quotes = False
                        current_item += char
                    elif char == ',' and not in_quotes:
                        items.append(current_item.strip())
                        current_item = ''
                    else:
                        current_item += char

                if current_item:
                    items.append(current_item.strip())

                result[key] = [_parse_scalar(item) for item in items]
        
        # Handle block list OR one-level nested mapping (empty value_part,
        # content on subsequent more-indented lines). NOTE: this used to be
        # two separate `elif not value_part and i + 1 < len(lines):`
        # branches -- since Python's elif chain only ever takes the FIRST
        # match, the second ("nested mapping") branch could never run at
        # all, silently turning every `metadata:\n  type: x\n  ...` block
        # into an empty string. Merged into one branch that checks the list
        # case first, then the nested-mapping case, then falls back to an
        # empty scalar.
        elif not value_part and i + 1 < len(lines):
            next_line = lines[i + 1]
            next_indent = len(next_line) - len(next_line.lstrip())
            key_indent = len(line) - len(line.lstrip())

            if next_indent > key_indent and next_line.lstrip().startswith('-'):
                # Collect list items
                items = []
                j = i + 1
                while j < len(lines):
                    item_line = lines[j]
                    item_indent = len(item_line) - len(item_line.lstrip())

                    if item_indent <= key_indent:
                        break

                    if item_line.lstrip().startswith('-'):
                        item_value = item_line[item_line.find('-') + 1:].lstrip()
                        items.append(_parse_scalar(item_value))

                    j += 1

                i = j - 1
                result[key] = items
            elif next_indent > key_indent:
                # One level of nested mapping.
                nested = {}
                j = i + 1
                while j < len(lines):
                    nested_line = lines[j]
                    nested_indent = len(nested_line) - len(nested_line.lstrip())

                    if nested_indent <= key_indent:
                        break

                    nested_colon = nested_line.find(':')
                    if nested_colon != -1:
                        nested_key = nested_line[:nested_colon].strip()
                        nested_value = nested_line[nested_colon + 1:].lstrip()
                        nested[nested_key] = _parse_scalar(nested_value)

                    j += 1

                i = j - 1
                result[key] = nested
            else:
                result[key] = _parse_scalar(value_part)

        else:
            result[key] = _parse_scalar(value_part)
        
        i += 1
    
    return result


def _parse_scalar(value: str) -> Any:
    """Parse a scalar value according to the YAML subset rules."""
    if not value:
        return ''
    
    # Remove inline comments (only if # is preceded by whitespace)
    if ' #' in value:
        parts = value.split(' #', 1)
        value = parts[0].rstrip()
    
    # Handle quoted strings
    if (value.startswith('"') and value.endswith('"')) or \
       (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    
    # Handle booleans and null (case-insensitive)
    lower_val = value.lower()
    if lower_val == 'true':
        return True
    elif lower_val == 'false':
        return False
    elif lower_val == 'null':
        return None
    
    # Handle integers
    try:
        # Check if it's a valid integer (no decimal point, no leading zeros unless just '0')
        if value.isdigit() or (value[0] == '-' and value[1:].isdigit()):
            return int(value)
    except (ValueError, IndexError):
        pass
    
    # Return as string
    return value
