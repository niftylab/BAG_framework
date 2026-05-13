# -*- coding: utf-8 -*-

import os

from bag.design.module import {{ module_name }}


# noinspection PyPep8Naming
class {{ lib_name }}__{{ cell_name }}({{ module_name }}):
    """design module for {{ lib_name }}__{{ cell_name }}.
    """

    yaml_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'netlist_info',
                             '{{ cell_name }}.yaml')

    def __init__(self, database, parent=None, prj=None, **kwargs):
        {{ module_name }}.__init__(self, database, self.yaml_file, parent=parent, prj=prj, **kwargs)
