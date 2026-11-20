"""Linear referencing for section-local measurements, in integer millimetres."""
from dataclasses import dataclass


def name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('expected nonempty identifier')
    return value


@dataclass(frozen=True)
class Section:
    offset: int
    length: int
    reverse: bool

    def project(self, start, end):
        if any(type(x) is not int for x in (start, end)) or start < 0 or start >= end or end > self.length:
            raise ValueError('invalid local span')
        if self.reverse:
            return self.offset + self.length - end, self.offset + self.length - start
        return self.offset + start, self.offset + end


def reference(rows):
    sections, offset = {}, 0
    for item in rows:
        section, length, direction = name(item['section']), item['length_mm'], item['direction']
        if section in sections or type(length) is not int or length <= 0 or direction not in ('forward', 'reverse'):
            raise ValueError('bad or duplicate section')
        sections[section] = Section(offset, length, direction == 'reverse')
        offset += length
        if offset >= 2**63:
            raise ValueError('route exceeds supported millimetre range')
    if not sections:
        raise ValueError('empty alignment')
    return sections
