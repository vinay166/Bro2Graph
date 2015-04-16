from bulbs.model import Node, Relationship
from bulbs.property import String, Integer, DateTime
from bulbs.utils import current_datetime

class Flow(Node):

    element_type = "flow"

    name = String(nullable=False)

class Source(Relationship):
    label = "source"
    element_type = "source"
    
class Dest(Relationship):
    label = "dest"
    element_type = "dest"
    