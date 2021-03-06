library(torch)
library(zeallot)


# Load data ---------------------------------------------------------------


dir <- "/tmp"

kmnist <- kmnist_dataset(
    dir,
    download = TRUE,
    transform = function(x) {
        x <- x$to(dtype = torch_float())/256
        x[newaxis,..]
    }
)
dl <- dataloader(kmnist, batch_size = 128, shuffle = TRUE)



# Model definition --------------------------------------------------------


device <- if (cuda_is_available()) torch_device("cuda:0") else "cpu"

image_size <- 28

view <- nn_module(
    "View",
    initialize = function(shape) {
        self$shape <- shape
    },
    forward = function(x) {
        x$view(self$shape)
    }
)

vae <- nn_module(
    "VAE",

    initialize = function(latent_dim) {
        self$latent_dim <- latent_dim
        self$latent_mean <- nn_linear(896, latent_dim)
        self$latent_log_var <- nn_linear(896, latent_dim)

        self$encoder <- nn_sequential(
            nn_conv2d(1, image_size, kernel_size= 3, stride= 2, padding  = 1),
            nn_batch_norm2d(image_size),
            nn_leaky_relu(),
            nn_conv2d(image_size, image_size * 2, kernel_size= 3, stride= 2, padding  = 1),
            nn_batch_norm2d(image_size * 2),
            nn_leaky_relu(),
            nn_conv2d(image_size * 2, image_size * 4, kernel_size= 3, stride= 2, padding  = 1),
            nn_batch_norm2d(image_size * 4),
            nn_leaky_relu(),
            nn_conv2d(image_size * 4, image_size * 8, kernel_size= 3, stride= 2, padding  = 1),
            nn_batch_norm2d(image_size * 8),
            nn_leaky_relu()
        )

        self$decoder <- nn_sequential(
            nn_linear(latent_dim, image_size * 8),
            view(c(-1, image_size * 8, 1, 1)),
            nn_conv_transpose2d(image_size * 8, image_size * 4, kernel_size = 4, stride = 1, padding = 0, bias = FALSE),
            nn_batch_norm2d(image_size * 4),
            nn_leaky_relu(),
            # 8 * 8
            nn_conv_transpose2d(image_size * 4, image_size * 2, kernel_size = 4, stride = 2, padding = 1, bias = FALSE),
            nn_batch_norm2d(image_size * 2),
            nn_leaky_relu(),
            # 16 x 16
            nn_conv_transpose2d(image_size * 2, image_size, kernel_size = 4, stride = 2, padding = 2, bias = FALSE),
            nn_batch_norm2d(image_size),
            nn_leaky_relu(),
            # 28 x 28
            nn_conv_transpose2d(image_size, 1, kernel_size = 4, stride = 2, padding = 1, bias = FALSE),
            nn_sigmoid()
        )
    },

    encode = function(x) {
        result <- self$encoder(x) %>%
            torch_flatten(start_dim = 1)
        mean <- self$latent_mean(result)
        log_var <- self$latent_log_var(result)
        list(mean, log_var)
    },

    decode = function(z) {
        self$decoder(z)
    },

    reparameterize = function(mean, logvar) {
        std <- torch_tensor(0.5, device = "cuda") * logvar
        eps <- torch_randn_like(std)
        eps * std + mean
    },

    loss_function = function(reconstruction, input, mean, log_var) {
        reconstruction_loss <- nnf_binary_cross_entropy(reconstruction, input, reduction = "sum")
        kl_loss <- torch_tensor(-0.5, device = "cuda") * torch_sum(torch_tensor(1, device = "cuda") + log_var - mean^2 - log_var$exp())
        loss <- reconstruction_loss + kl_loss
        list(loss, reconstruction_loss, kl_loss)
    },

    forward = function(x) {
        c(mean, log_var) %<-% self$encode(x)
        z <- self$reparameterize(mean, log_var)
        list (self$decode(z), x, mean, log_var)
    },

    sample = function(num_samples, current_device) {
        z <- torch_randn(num_samples, self$latent_dim)
        z <- z$to(device = current_device)
        samples <- self$decode(z)
        samples
    }

)

model <- vae(latent_dim = 2)$to(device = device)


# Train -------------------------------------------------------------------


optimizer <- optim_adam(model$parameters, lr = 0.001)

num_epochs <- 3

img_list <- vector(mode = "list", length = num_epochs * trunc(dl$.iter()$.length()/50))

img_num <- 0
for (epoch in 1:num_epochs) {

    batchnum <- 0
    for (b in enumerate(dl)) {

        batchnum <- batchnum + 1
        input <- b[[1]]$to(device = device)
        optimizer$zero_grad()
        c(reconstruction, input, mean, log_var) %<-% model(input)
        c(loss, reconstruction_loss, kl_loss) %<-% model$loss_function(reconstruction, input, mean, log_var)

        if(batchnum %% 50 == 0) {
            img_num <- img_num + 1
            cat("Epoch: ", epoch,
                "    batch: ", batchnum,
                "    loss: ", as.numeric(loss$cpu()),
                "    recon loss: ", as.numeric(reconstruction_loss$cpu()),
                "    KL loss: ", as.numeric(kl_loss$cpu()),
                "\n")
            with_no_grad({
                generated <- model$sample(64, device)
                grid <- vision_make_grid(generated)
                img_list[[img_num]] <- as_array(grid$to(device = "cpu"))
            })

        }
        loss$backward()
        optimizer$step()
    }
}



# Plot artifacts over time ------------------------------------------------


index <- seq(1, length(img_list), length.out = 16)
images <- img_list[index]

par(mfrow = c(4,4), mar = rep(0.2, 4))
rasterize <- function(x) {
    as.raster(x[1, , ])
}
images %>%
    purrr::map(rasterize) %>%
    purrr::iwalk(~{plot(.x)})



# Visualize latent space --------------------------------------------------

kmnist_test <- kmnist_dataset(
    dir,
    train = FALSE,
    download = TRUE,
    transform = function(x) {
        x <- x$to(dtype = torch_float())/256
        x[newaxis,..]
    }
)

dl_test <- dataloader(kmnist_test, batch_size = 10000, shuffle = FALSE)

model$eval()

with_no_grad({
    c(inputs, labels) %<-% dl_test$.iter()$.next()
    inputs <- inputs$to(device = device)
    encoded <- model$encode(inputs)
})

library(ggplot2)
library(dplyr)

encoded <- encoded[[1]]$cpu() %>% as_array()
labels <- as.integer(labels$cpu()$to(dtype = torch_int32()))
encoded %>%
    as.data.frame() %>%
    mutate(class = as.factor(labels)) %>%
    ggplot(aes(x = V1, y = V2, colour = class)) + geom_point() +
    coord_fixed(xlim = c(-4, 4), ylim = c(-4, 4))


# Visualize transitions ---------------------------------------------------

n <- 8

grid_x <- seq(-8, 8, length.out = n)
grid_y <- seq(-8, 8, length.out = n)

model$eval()

rows <- NULL
for(i in 1:length(grid_x)){
    column <- NULL
    for(j in 1:length(grid_y)){
        z_sample <- torch_tensor(c(grid_x[i], grid_y[j]))$cuda()
        column <- rbind(column, as_array(model$decode(z_sample)$cpu()$detach()[1, 1, , ]))
    }
    rows <- cbind(rows, column)
}
rows %>% as.raster() %>% plot()

