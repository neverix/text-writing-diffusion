import torch

def contains_cl(args):
    for t in args:
        if isinstance(t, torch.Tensor):
            if t.is_contiguous(memory_format=torch.channels_last) and not t.is_contiguous():
                return True
        elif isinstance(t, list) or isinstance(t, tuple):
            if contains_cl(list(t)):
                return True
    return False


def print_inputs(args, indent=""):
    for t in args:
        if isinstance(t, torch.Tensor):
            print(indent, t.stride(), t.shape, t.device, t.dtype)
        elif isinstance(t, list) or isinstance(t, tuple):
            print(indent, type(t))
            print_inputs(list(t), indent=indent + "    ")
        else:
            print(indent, t)


def check_wrapper(fn):
    name = fn.__name__ if hasattr(fn, '__name__') else 'noname__' + repr(fn)

    def check_cl(*args, **kwargs):
        was_cl = contains_cl(args)
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            print("`{}` inputs are:".format(name))
            print_inputs(args)
            print("-------------------")
            raise e
        failed = False
        if was_cl:
            if isinstance(result, torch.Tensor):
                if result.dim() == 4 and not result.is_contiguous(memory_format=torch.channels_last):
                    print(
                        "`{}` got channels_last input, but output is not channels_last:".format(name),
                        result.shape,
                        result.stride(),
                        result.device,
                        result.dtype,
                    )
                    failed = True
        if failed and True:
            print("`{}` inputs are:".format(name))
            print_inputs(args)
            msg = "Operator `{}` lost channels_last property".format(name)
            if false:
                raise Exception(msg)
            print(msg)
        return result

    return check_cl


old_attrs = dict()


def attribute(m):
    old_attrs[m] = dict()
    for i in dir(m):
        e = getattr(m, i)
        exclude_functions = ["is_cuda", "has_names", "numel", "stride", "Tensor", "is_contiguous", "__class__"]
        if i not in exclude_functions and not i.startswith("_") and "__call__" in dir(e):
            try:
                old_attrs[m][i] = e
                setattr(m, i, check_wrapper(e))
            except Exception as e:
                print(i)
                print(e)


attribute(torch.Tensor)
attribute(torch.nn.functional)
attribute(torch)
