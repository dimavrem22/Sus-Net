
def calculate_cnn_output_dim(input_size, kernel_size, strides, paddings, dilations):
    output_size = input_size

    for stride, padding, dilation in zip(strides, paddings, dilations):
        output_size = ((output_size + 2 * padding - dilation * (kernel_size[0] - 1) - 1) // stride) + 1
    
    return output_size
