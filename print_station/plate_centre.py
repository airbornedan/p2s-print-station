"""Keep prints off the worn centre of the plate (parts printed there come loose)."""


def centre_problem(bbox, plate, radius, margin=3):
    """Message if the print's footprint [x0, y0, x1, y1] touches the centre circle and could sit beside it; else None."""
    if not bbox or radius <= 0:
        return None
    x0, y0, x1, y1 = bbox
    c = plate / 2
    dx = max(x0 - c, 0, c - x1)
    dy = max(y0 - c, 0, c - y1)
    if dx * dx + dy * dy >= radius * radius:
        return None
    room = c - radius - margin   # width of the strip beside the circle
    if min(x1 - x0, y1 - y0) > room:
        return None              # too big to avoid the centre
    return "Print sits on the worn plate centre (within %d mm); drag it to one side." % radius
