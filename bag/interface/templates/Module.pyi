# -*- coding: utf-8 -*-

from typing import Dict

import os
import pkg_resources

from bag.design.module import Module


# noinspection PyPep8Naming
class {{ lib_name }}__{{ cell_name }}(Module):
    """Module for library {{ lib_name }} cell {{ cell_name }}.

    Fill in high level description here.
    """
    yaml_file = pkg_resources.resource_filename(__name__,
                                                os.path.join('netlist_info',
                                                             '{{ cell_name }}.yaml'))


    def __init__(self, database, parent=None, prj=None, **kwargs):
        Module.__init__(self, database, self.yaml_file, parent=parent, prj=prj, **kwargs)

    '''
    @classmethod
    def get_params_info(cls):
        # type: () -> Dict[str, str]
        """Returns a dictionary from parameter names to descriptions.

        Returns
        -------
        param_info : Optional[Dict[str, str]]
            dictionary from parameter names to descriptions.
        """
        return dict(
        )
    '''

    def design(self):
        """To be overridden by subclasses to design this module.

        This method should fill in values for all parameters in
        self.parameters.  To design instances of this module, you can
        call their design() method or any other ways you coded.

        To modify schematic structure, call:

        rename_pin(old_pin, new_pin)
        add_pin(new_pin, pin_type (input, output, inputOutput))
        remove_pin(remove_pin)
        delete_instance(inst_name)
        replace_instance_master(inst_name, lib_name, cell_name, static=False, index=None)
        reconnect_instance_terminal(inst_name, term_name, net_name, index=None)
        array_instance(inst_name, inst_name_list, term_list)
        restore_instance()
        """
        pass
