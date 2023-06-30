
def print_failure_unit_error(failure_unit):
    tb_text = failure_unit.traceback
    # TODO: add a try/except around importing pygments; if it is there,
    # let's make the output pretty
    print(tb_text)

def unit_summary(unit_result):
    ...

def result_summary(result_dict, output):
    import math
    # we were success or failure
    success = "FAILURE" if math.isnan(result_dict['estimate']) else "SUCCESS"
    yield f"This edge was a {success}."
    units = result_dict['unit_results']
    yield f"This edge consists of {len(units)} units."
    for unit in units:
        yield f"For unit ??unit label??"
        unit_summ = unit_summary(unit)
        ...



def inspect_result(json_filename):
    ...
