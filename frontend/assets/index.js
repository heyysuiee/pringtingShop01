/**
 * Printing Shop System - Home Page Interactivity
 * Features: FAQ Accordion, Smooth Anchor Scrolling, and Image Fallbacks
 */

document.addEventListener("DOMContentLoaded", () => {
    initFaqAccordion();
    initSmoothScroll();
    initImageFallbacks();
});

/**
 * Interactive FAQ Accordion
 */
function initFaqAccordion() {
    const faqItems = document.querySelectorAll(".faq-item");
    if (!faqItems.length) return;

    faqItems.forEach(item => {
        const questionBtn = item.querySelector(".faq-question");
        if (!questionBtn) return;

        questionBtn.addEventListener("click", () => {
            const isActive = item.classList.contains("active");

            // Close other items for a clean single-open accordion feel
            faqItems.forEach(otherItem => {
                if (otherItem !== item) {
                    otherItem.classList.remove("active");
                    const otherBtn = otherItem.querySelector(".faq-question");
                    if (otherBtn) otherBtn.setAttribute("aria-expanded", "false");
                }
            });

            // Toggle current item
            if (isActive) {
                item.classList.remove("active");
                questionBtn.setAttribute("aria-expanded", "false");
            } else {
                item.classList.add("active");
                questionBtn.setAttribute("aria-expanded", "true");
            }
        });
    });
}

/**
 * Smooth Scrolling for Anchor Links (#services, #how-it-works, #faq)
 */
function initSmoothScroll() {
    const anchorLinks = document.querySelectorAll('a[href^="#"]');
    anchorLinks.forEach(link => {
        link.addEventListener("click", function(e) {
            const targetId = this.getAttribute("href");
            if (targetId === "#") return;

            const targetElement = document.querySelector(targetId);
            if (targetElement) {
                e.preventDefault();
                targetElement.scrollIntoView({
                    behavior: "smooth",
                    block: "start"
                });

                // Update active state in nav if applicable
                document.querySelectorAll(".nav-menu a").forEach(a => a.classList.remove("active"));
                if (this.classList.contains("active") === false && this.closest(".nav-menu")) {
                    this.classList.add("active");
                }
            }
        });
    });
}

/**
 * Image Fallback Handler: Ensures images load seamlessly across web server & local filesystem
 */
function initImageFallbacks() {
    const images = document.querySelectorAll("img");
    images.forEach(img => {
        img.addEventListener("error", function() {
            if (!this.dataset.retried) {
                this.dataset.retried = "1";
                const currentSrc = this.getAttribute("src") || "";
                const cleanPath = currentSrc.replace(/^.*\/image\//, "image/");
                if (cleanPath && cleanPath !== currentSrc) {
                    this.src = cleanPath;
                }
            }
        });
    });
}

(() => {
    "use strict";

    /* ---------- Mobile menu ---------- */

    const menuToggle = document.querySelector("#menuToggle, .inner-page .nav-toggle");
    const navLinks = document.querySelector("#navLinks, .inner-page .nav-menu");

    if (menuToggle && navLinks) {
        const setMenuOpen = (isOpen) => {
            navLinks.classList.toggle("show", isOpen);
            navLinks.classList.toggle("open", isOpen);
            menuToggle.setAttribute("aria-expanded", String(isOpen));
            menuToggle.setAttribute("aria-label", isOpen ? "Close menu" : "Open menu");
            if (!menuToggle.querySelector("span")) {
                menuToggle.textContent = isOpen ? "×" : "☰";
            }
        };

        menuToggle.addEventListener("click", () => {
            setMenuOpen(menuToggle.getAttribute("aria-expanded") !== "true");
        });

        navLinks.addEventListener("click", (event) => {
            if (event.target.closest("a")) setMenuOpen(false);
        });

        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") setMenuOpen(false);
        });
    }

    /* ---------- Hero slider ---------- */

    const slider = document.querySelector("#heroSlider");
    if (slider) {
        const slides = [...slider.querySelectorAll(".image-container")];
        const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
        let activeIndex = 0;
        let autoplay;

        const showSlide = (index) => {
            activeIndex = (index + slides.length) % slides.length;
            slides.forEach((slide, slideIndex) => {
                const isActive = slideIndex === activeIndex;
                slide.classList.toggle("is-active", isActive);
                slide.setAttribute("aria-hidden", String(!isActive));
            });
        };

        const stopAutoplay = () => window.clearInterval(autoplay);

        const startAutoplay = () => {
            stopAutoplay();
            if (!reducedMotion.matches && !document.hidden) {
                autoplay = window.setInterval(() => showSlide(activeIndex + 1), 5000);
            }
        };

        slider.addEventListener("mouseenter", stopAutoplay);
        slider.addEventListener("mouseleave", startAutoplay);
        slider.addEventListener("focusin", stopAutoplay);
        slider.addEventListener("focusout", (event) => {
            if (!slider.contains(event.relatedTarget)) startAutoplay();
        });
        document.addEventListener("visibilitychange", startAutoplay);
        reducedMotion.addEventListener("change", startAutoplay);
        startAutoplay();
    }

    /* ---------- Service data (ids match order.js) ---------- */

    const services = {
        invitation: {
            label: "Invitation",
            title: "Invitation Printing",
            kicker: "For life's special moments",
            description: "Set the tone for birthdays, weddings, and christenings with a custom invitation design.",
            palette: ["#f1e5d7", "#60483a", "#b87968"],
            samples: [
                ["birthday-invitation", "Birthday Invitation", "A cheerful invitation sample for birthdays and milestones.", "/frontend/image/invitation/Bdayinvitation.jpg"],
                ["wedding-invitation", "Wedding Invitation", "An elegant stationery sample for wedding celebrations.", "/frontend/image/invitation/weddinginvitationj.png"],
                ["christening-invitation", "Christening Invitation", "A gentle invitation sample for a christening day.", "/frontend/image/invitation/christening.png"]
            ]
        },
        souvenir: {
            label: "Souvenir",
            title: "Personalized Souvenirs",
            kicker: "A keepsake to hold onto",
            description: "Personalize useful keepsakes for celebrations, gifts, and everyday moments.",
            palette: ["#eee2d2", "#4f4035", "#c6a15b"],
            samples: [
                ["refrigerator-magnet", "Refrigerator Magnet", "A personalized refrigerator magnet sample for a special occasion.", "/frontend/image/souvenir/refmagnet.png"],
                ["acrylic-keychain", "Acrylic Keychain", "A custom acrylic keychain sample with a personal design.", "/frontend/image/souvenir/keychain.jpg"],
                ["personalized-nail-cutter", "Personalized Nail Cutter", "A small personalized keepsake concept."],
                ["white-mug", "White Mug", "A custom print concept for a classic white mug.", "/frontend/image/souvenir/whitemug.png"],
                ["glass-mug", "Glass Mug", "A custom print sample for a personalized glass mug.", "/frontend/image/souvenir/glassmug.jpg"],
                ["nordic-mug", "Nordic Mug", "A personalized print concept for a Nordic-style mug."],
                ["personalized-tote-bag", "Personalized Tote Bag", "A custom tote concept for gifts or everyday use.", "/frontend/image/souvenir/totebag.png"],
                ["coloring-book", "Coloring Book", "A custom cover concept for a personalized coloring book."]
            ]
        },
        sticker: {
            label: "Sticker Printing",
            title: "Sticker Printing",
            kicker: "Make your mark",
            description: "Custom sticker concepts for product packaging, business branding, and personal projects.",
            palette: ["#e8e7dc", "#3e5147", "#b87968"],
            samples: [
                ["business-logo-stickers", "Business Logo Stickers", "Brand sticker concept; shape, finish, and size can be discussed."],
                ["product-label-stickers", "Product Label Stickers", "Label concept for product packaging and small businesses."],
                ["name-stickers", "Name Stickers", "Personalized name sticker concept for school, gifts, or events."],
                ["wall-stickers-decals", "Wall Stickers / Decals", "Decorative decal concept for a room or display."]
            ]
        },
        "sintra-board": {
            label: "Sintra Board",
            title: "Sintra Board Printing",
            kicker: "Make the moment stand out",
            description: "Display board concepts for celebrations, menus, photos, and special spaces.",
            palette: ["#e8e4d7", "#4e4a3b", "#8e9a78"],
            samples: [
                ["birthday-welcome-signs", "Birthday / Welcome Signs", "A welcome sign sample for parties and celebrations.", "/frontend/image/SintraBoard/welcomeBoard.jpg"],
                ["wedding-signs", "Wedding Signs", "A coordinated sign sample for wedding venues and receptions.", "/frontend/image/SintraBoard/welcomeBoard.jpg"],
                ["menu-board", "Menu Board", "A clear, custom menu board sample for an event or business.", "/frontend/image/SintraBoard/menuBoard.png"],
                ["photo-mounting-boards", "Photo Mounting Boards", "A mounted photo presentation sample.", "/frontend/image/SintraBoard/customizePhotoDesign.png"],
                ["customized-photo-displays", "Customized Photo Displays", "A personalized photo display sample.", "/frontend/image/SintraBoard/customizePhotoDesign.png"],
                ["photo-collages", "Photo Collages", "A composed collage sample for memories and milestones.", "/frontend/image/SintraBoard/photocollage.png"]
            ]
        },
        "party-needs": {
            label: "Party Needs",
            title: "Personalized Party Needs",
            kicker: "Add a little celebration",
            description: "Coordinate the small details with personalized party pieces made for your event.",
            palette: ["#f0e0da", "#5a3d3a", "#d7a6a1"],
            samples: [
                ["party-hats", "Party Hats", "A coordinated party hat concept with personalized colors."],
                ["gable-boxes", "Gable Boxes", "A custom favor box concept for guests and giveaways."],
                ["gift-thank-you-tags", "Gift / Thank You Tags", "A tag concept for gifts, favors, and thank-you notes."],
                ["personalized-candles", "Personalized Candles", "A personalized candle label concept for an event."]
            ]
        },
        "large-format": {
            label: "Tarpaulins & Large Format",
            title: "Tarpaulins & Large Format Printing",
            kicker: "Big prints, made clear",
            description: "Large-format concepts for events, storefronts, announcements, and wayfinding.",
            palette: ["#e2e8e6", "#344b4c", "#c6a15b"],
            samples: [
                ["birthday-wedding-graduation-banners", "Birthday, Wedding & Graduation Banners", "A large-format event banner sample; final dimensions and material require a quote.", "/frontend/image/tarpaulin/event.png"],
                ["store-banners-promotions", "Store Banners & Promotions", "Promotional banner concept for a store or small business."],
                ["product-advertisements", "Product Advertisements", "Large-format product advertising concept."],
                ["stage-backdrops", "Stage Backdrops", "Backdrop concept for a stage, event, or photo area."],
                ["welcome-banners", "Welcome Banners", "A welcome banner concept for guests and event venues."],
                ["program-banners", "Program Banners", "A banner concept for event programs and schedules."],
                ["school-announcements-schedules", "School Announcements & Schedules", "Announcement or schedule layout concept."],
                ["public-notices", "Public Notices", "A readable public notice layout concept."],
                ["entrance-signs", "Entrance Signs", "A custom entrance sign concept for a venue or event."],
                ["directional-banners", "Directional Banners", "A wayfinding banner concept to guide visitors."],
                ["temporary-signs", "Temporary Signs", "A short-term sign concept for events and promotions."]
            ]
        }
    };

    /* ---------- Live catalog (administrator-managed) ----------
       The list above is only a fallback for when the API cannot be reached. Normally the pages show the
       categories and products from /api/catalog, so admin changes appear here without code edits. */

    const presentation = Object.fromEntries(Object.entries(services).map(([id, service]) => [
        id, { title: service.title, kicker: service.kicker, palette: service.palette }
    ]));
    const defaultPresentation = { kicker: "Made to order", palette: ["#e8e7dc", "#3e5147", "#b87968"] };
    const peso = (value) => new Intl.NumberFormat("en-PH", { style: "currency", currency: "PHP" }).format(value);

    function priceLabel(product) {
        if (product.pricing_mode === "quote" || product.base_price === null) return "Pricing by quote";
        return product.pricing_mode === "area" ? `${peso(product.base_price)} per sq ft` : `From ${peso(product.base_price)} each`;
    }

    function applyCatalog(categories) {
        Object.keys(services).forEach((key) => delete services[key]);
        categories.forEach((category) => {
            const look = presentation[category.id] || defaultPresentation;
            services[category.id] = {
                label: category.label,
                title: look.title || category.label,
                kicker: look.kicker,
                description: category.description || "",
                palette: look.palette,
                samples: category.products.map((product) => [
                    product.id, product.name, product.description, product.image || undefined, priceLabel(product)
                ])
            };
        });
    }

    /* ---------- Elements ---------- */

    const serviceCategories = document.querySelector("#service-categories");
    const portfolioGrid = document.querySelector("#portfolio-grid");
    const portfolioCount = document.querySelector("#result-count");
    const sampleDialog = document.querySelector("#sample-dialog");
    const dialogImage = document.querySelector("#sample-dialog-image");
    const dialogCategory = document.querySelector("#sample-dialog-category");
    const dialogTitle = document.querySelector("#sample-dialog-title");
    const dialogDescription = document.querySelector("#sample-dialog-description");
    const dialogRequest = document.querySelector("#sample-dialog-request");
    const dialogPrice = sampleDialog?.querySelector(".demo-label");
    let portfolioFilter = "all";

    /* ---------- Helpers ---------- */

    const orderUrl = (serviceId, designId) => {
        const params = new URLSearchParams({ service: serviceId });
        if (designId) params.set("design", designId);
        return "/order?" + params.toString();
    };

    const svgNamespace = "http://www.w3.org/2000/svg";
    const createSvgElement = (tagName, attributes = {}) => {
        const element = document.createElementNS(svgNamespace, tagName);
        Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, value));
        return element;
    };

    function artFor(serviceId, sample, index) {
        const service = services[serviceId];
        const [background, ink, accent] = service.palette;
        const tilt = index % 2 ? 4 : -4;
        const svg = createSvgElement("svg", { viewBox: "0 0 720 520" });
        svg.append(
            createSvgElement("rect", { width: 720, height: 520, fill: background }),
            createSvgElement("circle", { cx: index % 2 ? 610 : 100, cy: 85, r: 112, fill: accent, opacity: ".22" }),
            createSvgElement("circle", { cx: index % 2 ? 70 : 650, cy: 450, r: 150, fill: ink, opacity: ".08" }),
            createSvgElement("rect", { x: 154, y: 66, width: 412, height: 388, rx: 14, fill: "#fffdf8", transform: `rotate(${tilt} 360 260)` }),
            createSvgElement("rect", { x: 207, y: 130, width: 306, height: 188, rx: 8, fill: background, transform: `rotate(${tilt} 360 260)` }),
            createSvgElement("path", { d: "M248 280l66-78 48 48 47-55 75 85H248z", fill: accent, opacity: ".85", transform: `rotate(${tilt} 360 260)` }),
            createSvgElement("circle", { cx: 438, cy: 188, r: 22, fill: ink, opacity: ".8", transform: `rotate(${tilt} 360 260)` })
        );

        const title = createSvgElement("text", {
            x: 360,
            y: 370,
            fill: ink,
            "font-family": "Georgia,serif",
            "font-size": 25,
            "text-anchor": "middle"
        });
        title.textContent = sample.name;

        const category = createSvgElement("text", {
            x: 360,
            y: 405,
            fill: ink,
            opacity: ".68",
            "font-family": "Arial,sans-serif",
            "font-size": 12,
            "letter-spacing": 3,
            "text-anchor": "middle"
        });
        category.textContent = `${service.label.toUpperCase()} · SAMPLE ART`;
        svg.append(title, category);

        const artwork = new XMLSerializer().serializeToString(svg);
        return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(artwork)}`;
    }

    function getSamples(serviceId) {
        return services[serviceId].samples.map(([id, name, description, imagePath, price], index) => ({
            id,
            name,
            description,
            price: price || "Pricing by quote",
            image: imagePath || artFor(serviceId, { name }, index),
            isProvidedImage: Boolean(imagePath)
        }));
    }

    window.PrintShopCatalog = services;
    window.PrintShopGetSamples = getSamples;

    const altFor = (sample) =>
        sample.isProvidedImage
            ? `Sample photo for ${sample.name}`
            : `Temporary sample artwork for ${sample.name}`;

    /* ---------- Sample cards ---------- */

    function createSampleCard(serviceId, sample, index, showActions = true) {
        const card = document.createElement("article");
        card.className = "sample-card";
        card.dataset.category = serviceId;

        const image = document.createElement("img");
        image.className = "sample-card-image";
        image.src = sample.image;
        image.alt = altFor(sample);
        image.loading = "lazy";

        const copy = document.createElement("div");
        copy.className = "sample-card-copy";

        const category = document.createElement("p");
        category.className = "sample-card-category";
        category.textContent = services[serviceId].label;

        const title = document.createElement("h4");
        title.textContent = sample.name;

        const description = document.createElement("p");
        description.textContent = sample.description;

        copy.append(category, title, description);

        if (showActions) {
            const price = document.createElement("p");
            price.className = "sample-card-price";
            price.textContent = sample.price;
            copy.append(price);
            const actions = document.createElement("div");
            actions.className = "sample-card-actions";

            const detailsButton = document.createElement("button");
            detailsButton.type = "button";
            detailsButton.className = "sample-card-link";
            detailsButton.textContent = "View details";
            detailsButton.addEventListener("click", () => openDetails(serviceId, sample));

            const orderButton = document.createElement("a");
            orderButton.className = "sample-card-link sample-card-request";
            orderButton.textContent = "Request this design";
            orderButton.href = orderUrl(serviceId, sample.id);

            actions.append(detailsButton, orderButton);
            copy.append(actions);
        }

        card.append(image, copy);
        return card;
    }

    /* ---------- Service categories (all on one page) ---------- */

    function createCategorySection(serviceId) {
        const service = services[serviceId];

        const section = document.createElement("section");
        section.className = "service-category";
        section.id = `service-${serviceId}`;
        section.setAttribute("aria-labelledby", `service-${serviceId}-title`);

        const heading = document.createElement("div");
        heading.className = "service-category-heading";

        const kicker = document.createElement("p");
        kicker.className = "showcase-kicker";
        kicker.textContent = service.kicker;

        const title = document.createElement("h3");
        title.id = `service-${serviceId}-title`;
        title.textContent = service.title;

        const description = document.createElement("p");
        description.textContent = service.description;

        const orderLink = document.createElement("a");
        orderLink.className = "showcase-btn";
        orderLink.href = orderUrl(serviceId);
        orderLink.textContent = "Order now";

        heading.append(kicker, title, description, orderLink);

        const grid = document.createElement("div");
        grid.className = "sample-grid";
        grid.append(
            ...getSamples(serviceId).map((sample, index) => createSampleCard(serviceId, sample, index))
        );

        section.append(heading, grid);
        return section;
    }

    function renderServiceCategories() {
        if (!serviceCategories) return;
        serviceCategories.replaceChildren(...Object.keys(services).map(createCategorySection));
    }

    /* ---------- Sample details dialog ---------- */

    function openDetails(serviceId, sample) {
        if (!sampleDialog) return;

        dialogImage.src = sample.image;
        dialogImage.alt = altFor(sample);
        dialogCategory.textContent = services[serviceId].label;
        dialogTitle.textContent = sample.name;
        dialogDescription.textContent = sample.description;
        if (dialogPrice) dialogPrice.textContent = `${sample.price} · final amount confirmed by the shop`;
        dialogRequest.href = orderUrl(serviceId, sample.id);
        sampleDialog.showModal();
    }

    document.querySelectorAll("[data-close-dialog]").forEach((button) => {
        button.addEventListener("click", () => button.closest("dialog")?.close());
    });

    /* ---------- Portfolio page ---------- */

    function renderPortfolio(filter = "all") {
        if (!portfolioGrid) return;

        const cards = [];
        Object.keys(services).forEach((serviceId) => {
            if (filter !== "all" && filter !== serviceId) return;
            getSamples(serviceId).forEach((sample, index) => {
                cards.push(createSampleCard(serviceId, sample, index, false));
            });
        });
        portfolioGrid.replaceChildren(...cards);
        if (portfolioCount) {
            portfolioCount.textContent = `Showing ${cards.length} sample ${cards.length === 1 ? "design" : "designs"}`;
        }
    }

    document.querySelectorAll("[data-portfolio-filter]").forEach((button) => {
        button.addEventListener("click", () => {
            document.querySelectorAll("[data-portfolio-filter]").forEach((filterButton) => {
                const selected = filterButton === button;
                filterButton.classList.toggle("active", selected);
                filterButton.setAttribute("aria-pressed", String(selected));
            });
            portfolioFilter = button.dataset.portfolioFilter;
            renderPortfolio(portfolioFilter);
        });
    });

    /* ---------- Start ---------- */

    async function start() {
        if (serviceCategories || portfolioGrid) {
            try {
                const response = await fetch("/api/catalog", { cache: "no-store" });
                if (response.ok) applyCatalog((await response.json()).categories);
            } catch (error) {
                console.warn("The live catalog could not be loaded; showing the built-in list.", error);
            }
        }
        renderServiceCategories();
        renderPortfolio(portfolioFilter);
    }

    void start();
})();